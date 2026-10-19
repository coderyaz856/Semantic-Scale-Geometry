"""Versioned, resumable orchestration for complete experiments."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import manifest
from .config import ExperimentConfig, load

STATUS_FORMAT = "semantic-scale-geometry/run-status/1"
STAGE_FORMAT = "semantic-scale-geometry/stage/1"
STATES = (
    "CREATED",
    "VALIDATED",
    "EXTRACTING",
    "MEASURING",
    "ANALYSING",
    "REPORTING",
    "SUCCEEDED",
    "FAILED",
)


class RunError(RuntimeError):
    """Raised when a run cannot safely continue."""


@dataclass(frozen=True)
class RunResult:
    run_id: str
    run_dir: Path
    reused: bool


class EventLog:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id

    def write(self, event: str, **fields: object) -> None:
        record = {
            "timestamp": manifest.utc_now(),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()


class RunLock:
    """A cross-platform advisory lock released automatically after a crash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        try:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            self.stream = None
            raise RunError(f"run is already active: {self.path.parent.name}") from error
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.stream is None:
            return
        if os.name == "nt":
            import msvcrt

            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def _resolve_executable(value: str) -> str:
    candidate = Path(value)
    if not candidate.exists() and candidate.with_suffix(".exe").exists():
        return str(candidate.with_suffix(".exe"))
    return value


def _publish_directory(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RunError(f"refusing to replace existing run output: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def _publish_file(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RunError(f"refusing to replace existing run output: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def _summary(path: Path) -> dict[str, Any]:
    if path.is_dir():
        return {"kind": "directory", **manifest.tree_summary(path)}
    if path.is_file():
        return {
            "kind": "file",
            "sha256": manifest.file_digest(path),
            "bytes": path.stat().st_size,
        }
    raise RunError(f"expected output does not exist: {path}")


def _valid_summary(path: Path, expected: dict[str, Any]) -> bool:
    try:
        return _summary(path) == expected
    except (OSError, RunError):
        return False


def _files(path: Path, pattern: str) -> list[Path]:
    if not path.is_dir():
        raise RunError(f"stage did not create an output directory: {path}")
    files = sorted(path.glob(pattern))
    if not files:
        raise RunError(f"stage produced no files matching {pattern}: {path}")
    return files


class Operations:
    """The side-effecting pipeline stages, replaceable in offline tests."""

    @staticmethod
    def _command(command: list[str], log: EventLog) -> None:
        command = [_resolve_executable(command[0]), *command[1:]]
        log.write("command_started", command=command)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise RunError(f"could not execute {command[0]}: {error}") from error
        log.write(
            "command_finished",
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if completed.returncode != 0:
            raise RunError(
                f"command failed with status {completed.returncode}: {command[0]}"
            )

    def extract(self, config: ExperimentConfig, destination: Path) -> None:
        from .extract import ExtractionConfig, load_background, write_shards
        from .scales import enumerate_rows, load_scales

        rows = enumerate_rows(load_scales(config.scales_path))
        background = load_background(config.background_path)
        extraction = ExtractionConfig(
            model=config.model.model_id,
            revision=config.model.revision,
            max_length=config.model.max_length,
            batch_size=config.model.batch_size,
            device=config.model.device,
            pooling=config.model.pooling,
        )
        written = write_shards(destination, rows, background, extraction)
        if not written:
            raise RunError("extraction produced no layer shards")

    def measure(
        self,
        config: ExperimentConfig,
        shards: Path,
        measurements: Path,
        directions: Path,
        log: EventLog,
    ) -> None:
        layer_dirs = sorted(path for path in shards.iterdir() if path.is_dir())
        if not layer_dirs:
            raise RunError("no layer shards are available for measurement")
        measurements.mkdir(parents=True)
        directions.mkdir(parents=True)
        removals = ",".join(str(value) for value in config.analysis.removals)
        for layer in layer_dirs:
            self._command(
                [
                    config.execution.kernel_binary,
                    "--shard",
                    str(layer),
                    "--out",
                    str(measurements / f"{layer.name}.json"),
                    "--remove-top",
                    removals,
                    "--random-draws",
                    str(config.analysis.random_draws),
                    "--bootstrap-draws",
                    str(config.analysis.bootstrap_draws),
                    "--seed",
                    str(config.analysis.seed),
                    "--directions",
                    str(directions / layer.name),
                    "--null-replicates",
                    str(config.analysis.null_replicates),
                ],
                log,
            )

    def analyse(
        self,
        config: ExperimentConfig,
        directions: Path,
        destination: Path,
        log: EventLog,
    ) -> None:
        inputs = sorted(path for path in directions.iterdir() if path.is_dir())
        if not inputs:
            raise RunError("no direction exports are available for analysis")
        self._command(
            [
                config.execution.subspace_binary,
                *[str(path) for path in inputs],
                "--out",
                str(destination),
                "--condition",
                config.analysis.subspace_condition,
            ],
            log,
        )

    def report(
        self,
        config: ExperimentConfig,
        measurements: Path,
        destination: Path,
        log: EventLog,
    ) -> None:
        inputs = sorted(measurements.glob("*.json"))
        if not inputs:
            raise RunError("no measurement files are available for reporting")
        command = [
            sys.executable,
            config.execution.report_script,
            *[str(path) for path in inputs],
            "--scales",
            str(config.scales_path),
        ]
        log.write("command_started", command=command)
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        log.write(
            "command_finished",
            command=command,
            returncode=completed.returncode,
            stderr=completed.stderr,
        )
        if completed.returncode != 0:
            raise RunError(f"report command failed with status {completed.returncode}")
        destination.write_text(completed.stdout, encoding="utf-8")


class Runtime:
    def __init__(
        self,
        config: ExperimentConfig,
        run_root: Path,
        operations: Operations,
    ) -> None:
        self.config = config
        self.inputs = {
            "scales_sha256": manifest.file_digest(config.scales_path),
            "background_sha256": manifest.file_digest(config.background_path),
        }
        self.run_id = manifest.run_id(config.normalized, self.inputs)
        self.run_dir = run_root.resolve() / self.run_id
        self.markers = self.run_dir / ".stages"
        self.operations = operations
        self.log = EventLog(self.run_dir / "logs.jsonl", self.run_id)
        self.manifest_path = self.run_dir / "manifest.json"
        self.status_path = self.run_dir / "status.json"
        self.document: dict[str, Any] = {}

    def _status(self, state: str, **fields: object) -> None:
        if state not in STATES:
            raise AssertionError(f"unknown run state: {state}")
        updated = manifest.utc_now()
        if state == "SUCCEEDED":
            self.document["completed_utc"] = updated
        duration = sum(
            float(stage.get("duration_seconds", 0.0))
            for stage in self.document.get("stages", {}).values()
        )
        error = fields.get("error")
        value = {
            "format": STATUS_FORMAT,
            "run_id": self.run_id,
            "state": state,
            "created_at": self.document["created_utc"],
            "completed_at": self.document.get("completed_utc"),
            "duration_seconds": round(duration, 6),
            "error_code": error.get("type") if isinstance(error, dict) else None,
            "updated_utc": updated,
            **fields,
        }
        manifest.write_json(value, self.status_path)
        self.document["state"] = state
        self.document["error"] = error
        manifest.write(self.document, self.manifest_path)
        self.log.write("state_changed", state=state, **fields)

    def _marker_path(self, name: str) -> Path:
        return self.markers / f"{name}.json"

    def _stage_complete(self, name: str, outputs: list[Path]) -> bool:
        marker_path = self._marker_path(name)
        if not marker_path.is_file():
            return False
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker.get("format") != STAGE_FORMAT:
                return False
            expected = marker["outputs"]
        except (OSError, KeyError, json.JSONDecodeError):
            return False
        keys = {str(path.relative_to(self.run_dir)) for path in outputs}
        if set(expected) != keys:
            return False
        return all(
            _valid_summary(path, expected[str(path.relative_to(self.run_dir))])
            for path in outputs
        )

    def _record_stage(
        self,
        name: str,
        started: float,
        outputs: list[Path],
    ) -> None:
        summaries = {
            str(path.relative_to(self.run_dir)): _summary(path) for path in outputs
        }
        duration = time.monotonic() - started
        record = {
            "format": STAGE_FORMAT,
            "stage": name,
            "completed_utc": manifest.utc_now(),
            "duration_seconds": round(duration, 6),
            "outputs": summaries,
        }
        manifest.write_json(record, self._marker_path(name))
        self.document["stages"][name] = record
        manifest.write(self.document, self.manifest_path)
        self.log.write("stage_completed", stage=name, duration_seconds=duration)

    def _prepare_stage(self, name: str, outputs: list[Path]) -> bool:
        if self._stage_complete(name, outputs):
            self.log.write("stage_reused", stage=name)
            return True
        marker = self._marker_path(name)
        if marker.exists():
            raise RunError(f"completed stage has modified outputs: {name}")
        for output in outputs:
            if output.is_dir():
                shutil.rmtree(output)
                self.log.write(
                    "orphaned_output_removed",
                    stage=name,
                    path=str(output.relative_to(self.run_dir)),
                )
            elif output.exists():
                output.unlink()
                self.log.write(
                    "orphaned_output_removed",
                    stage=name,
                    path=str(output.relative_to(self.run_dir)),
                )
        return False

    def _temporary(self, label: str) -> Path:
        return self.run_dir / f".{label}.{uuid.uuid4().hex}.tmp"

    def _prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.markers.mkdir(exist_ok=True)
        if self.manifest_path.exists():
            self.document = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if self.document.get("run_id") != self.run_id:
                raise RunError("existing manifest has a different run identity")
            if self.document.get("config") != self.config.normalized:
                raise RunError("existing manifest has a different configuration")
            if self.document.get("inputs") != self.inputs:
                raise RunError("existing manifest has different input identities")
            return
        self.document = manifest.build(
            self.config.normalized,
            identity=self.run_id,
            inputs=self.inputs,
        )
        manifest.write(self.document, self.manifest_path)
        normalized = yaml.safe_dump(self.config.normalized, sort_keys=False)
        config_path = self.run_dir / "config.yaml"
        temporary = config_path.with_name(".config.yaml.tmp")
        temporary.write_text(normalized, encoding="utf-8")
        os.replace(temporary, config_path)
        self._status("CREATED")

    def execute(self) -> RunResult:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with RunLock(self.run_dir / ".run.lock"):
            return self._execute_locked()

    def _execute_locked(self) -> RunResult:
        self._prepare()
        final_outputs = [
            self.run_dir / "shards",
            self.run_dir / "measurements",
            self.run_dir / "directions",
            self.run_dir / "report" / "subspace.json",
            self.run_dir / "report" / "summary.txt",
        ]
        completed_stages = (
            self._stage_complete("extraction", final_outputs[:1])
            and self._stage_complete("measurement", final_outputs[1:3])
            and self._stage_complete("analysis", final_outputs[3:4])
            and self._stage_complete("report", final_outputs[4:])
        )
        if self.document.get("state") == "SUCCEEDED":
            if completed_stages:
                self.log.write("run_reused")
                return RunResult(self.run_id, self.run_dir, True)
            raise RunError("a successful run has missing or modified immutable outputs")

        try:
            self._verify_inputs()
            self._status("VALIDATED")
            self._extract()
            self._measure()
            self._analyse()
            self._report()
            self.document["outputs"] = {
                str(path.relative_to(self.run_dir)): _summary(path)
                for path in final_outputs
            }
            self._verify_inputs()
            self._status("SUCCEEDED")
        except Exception as error:
            self._status(
                "FAILED",
                error={"type": type(error).__name__, "message": str(error)},
            )
            raise
        return RunResult(self.run_id, self.run_dir, False)

    def _verify_inputs(self) -> None:
        current = {
            "scales_sha256": manifest.file_digest(self.config.scales_path),
            "background_sha256": manifest.file_digest(self.config.background_path),
        }
        if current != self.inputs:
            raise RunError("an input file changed while the run was active")

    def _extract(self) -> None:
        destination = self.run_dir / "shards"
        if self._prepare_stage("extraction", [destination]):
            return
        self._status("EXTRACTING")
        started = time.monotonic()
        temporary = self._temporary("shards")
        try:
            self.operations.extract(self.config, temporary)
            layers = _files(temporary, "layer_*")
            for layer in layers:
                json.loads((layer / "shard.json").read_text(encoding="utf-8"))
                if not (layer / "embeddings.f32").is_file():
                    raise RunError(f"shard has no vector payload: {layer}")
            _summary(temporary)
            _publish_directory(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        self._record_stage("extraction", started, [destination])

    def _measure(self) -> None:
        outputs = [self.run_dir / "measurements", self.run_dir / "directions"]
        if self._prepare_stage("measurement", outputs):
            return
        self._status("MEASURING")
        started = time.monotonic()
        root = self._temporary("measurement")
        measurements = root / "measurements"
        directions = root / "directions"
        try:
            self.operations.measure(
                self.config,
                self.run_dir / "shards",
                measurements,
                directions,
                self.log,
            )
            measurement_files = _files(measurements, "*.json")
            direction_dirs = _files(directions, "layer_*")
            shard_dirs = _files(self.run_dir / "shards", "layer_*")
            if len(measurement_files) != len(shard_dirs):
                raise RunError("measurement count does not match the shard count")
            if len(direction_dirs) != len(shard_dirs):
                raise RunError("direction export count does not match the shard count")
            for path in measurement_files:
                json.loads(path.read_text(encoding="utf-8"))
            for path in direction_dirs:
                json.loads((path / "directions.json").read_text(encoding="utf-8"))
                if not (path / "directions.f32").is_file():
                    raise RunError(f"direction export has no vector payload: {path}")
            _summary(measurements)
            _summary(directions)
            _publish_directory(measurements, outputs[0])
            _publish_directory(directions, outputs[1])
        finally:
            if root.exists():
                shutil.rmtree(root)
        self._record_stage("measurement", started, outputs)

    def _analyse(self) -> None:
        destination = self.run_dir / "report" / "subspace.json"
        if self._prepare_stage("analysis", [destination]):
            return
        self._status("ANALYSING")
        started = time.monotonic()
        temporary = self._temporary("subspace")
        try:
            self.operations.analyse(
                self.config,
                self.run_dir / "directions",
                temporary,
                self.log,
            )
            json.loads(temporary.read_text(encoding="utf-8"))
            _publish_file(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        self._record_stage("analysis", started, [destination])

    def _report(self) -> None:
        destination = self.run_dir / "report" / "summary.txt"
        if self._prepare_stage("report", [destination]):
            return
        self._status("REPORTING")
        started = time.monotonic()
        temporary = self._temporary("report")
        try:
            self.operations.report(
                self.config,
                self.run_dir / "measurements",
                temporary,
                self.log,
            )
            if not temporary.is_file():
                raise RunError("report stage did not write its output")
            if temporary.stat().st_size == 0:
                raise RunError("report stage wrote an empty output")
            _publish_file(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        self._record_stage("report", started, [destination])


def run_experiment(
    config_path: str | Path,
    run_root: str | Path,
    operations: Operations | None = None,
) -> RunResult:
    config = load(config_path)
    return Runtime(config, Path(run_root), operations or Operations()).execute()
