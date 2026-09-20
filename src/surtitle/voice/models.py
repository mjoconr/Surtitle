"""Local speech models: where they come from, where they live, and are they intact.

A local voice engine is only as good as the files underneath it, and those files
are large binaries that the application does not ship. That makes three things
load-bearing, and each of them is a bug this module exists to prevent:

1. **A checksum.** A truncated or corrupted ONNX file does not raise — it
   produces *garbage transcripts*, which reads like a bad model rather than a
   bad download. Every file is verified against a pinned SHA-256, and a mismatch
   deletes the artifact and names the URL.
2. **Atomicity.** Downloads are staged in a private directory and moved into
   place only once the model is complete, so an interrupted install can never be
   mistaken for a usable model by the next start.
3. **Consent.** Nothing here runs as a side effect of starting the app. Model
   downloads happen when the user asks for them, from Settings or
   ``surtitle models download``, because quietly pulling ~85 MB over their
   connection is not the application's decision to make.

Archives come off the network, so extraction validates each member path before
writing anything (``_safe_extract``). A tar member named ``../../bin/sh`` is a
path-traversal write, not a model file.

Every checksum and size below was produced by downloading the archive and
hashing the extracted files on the development machine, so a mismatch means the
download is wrong rather than the table is.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from surtitle.config import Settings

__all__ = [
    "MODEL_REGISTRY",
    "ModelAsset",
    "ModelFile",
    "ModelStatus",
    "ModelUnavailable",
    "describe",
    "download",
    "iter_assets",
    "missing_files",
    "model_keys",
    "models_dir",
    "registry_key",
    "resolve_stt",
    "resolve_tts",
    "status",
    "verify",
]

# GitHub release assets, with a Hugging Face mirror as the second source for
# networks where github.com release downloads are blocked or rate-limited.
_GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
_STT_RELEASE = "asr-models"
_TTS_RELEASE = "tts-models"
_HF_PREFIX = "https://huggingface.co/csukuangfj/sherpa-onnx-models/resolve/main"

_DOWNLOAD_TIMEOUT = 60.0
_CHUNK = 1 << 20


class ModelUnavailable(RuntimeError):
    """A local model cannot be used, with a reason and a fix for the UI.

    Raised where the caller can report it — while building the engine, or in a
    doctor check — rather than deep inside inference, because by then the only
    symptom is a microphone that produces nothing.
    """

    def __init__(self, reason: str, *, fix: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


@dataclass(slots=True, frozen=True)
class ModelFile:
    """One file inside an archive, pinned by SHA-256 and size.

    The size is recorded so a truncated download is caught before it is hashed:
    reading 70 MB only to compute a digest that was never going to match is a
    slow way to report a dropped connection.
    """

    name: str
    sha256: str
    size: int
    # True for the files the engine is actually handed, which is what a
    # user-facing summary should name.
    primary: bool = False
    # Everything in the archive except this file can be thrown away without the
    # installer ever unpacking it. Worth setting for large variants that are
    # shipped but not downloaded — see `ModelAsset.files`.
    optional: bool = False


@dataclass(slots=True, frozen=True)
class ModelAsset:
    """A downloadable model: one archive, several required files."""

    key: str
    kind: str  # "stt" | "tts"
    label: str
    archive: str
    release: str
    files: tuple[ModelFile, ...]
    # Root directory inside the archive that holds ``files``.
    strip_prefix: str = ""
    # Directory trees (not single files) that must exist after extraction.
    # Members are no longer extracted wholesale, so this only asserts presence:
    # the installer writes the files it needs and creates the trees around them.
    requires_dirs: tuple[str, ...] = ()
    notes: str = ""

    @property
    def urls(self) -> tuple[str, ...]:
        """Download locations, in the order they are tried."""
        return (
            f"{_GH}/{self.release}/{self.archive}",
            f"{_HF_PREFIX}/{self.release}/{self.archive}",
        )

    @property
    def total_bytes(self) -> int:
        """Bytes actually needed on disk once installed.

        Optional members are excluded, because they are deliberately not
        downloaded: counting them overstated the figure quoted in the install
        prompt by 260 MB for the one model that ships an fp32 alternative it
        never uses.
        """
        return sum(f.size for f in self.files if not f.optional)

    def find(self, name: str) -> ModelFile | None:
        """The required file with this exact name."""
        for entry in self.files:
            if entry.name == name:
                return entry
        return None


def _f(
    name: str,
    sha256: str,
    size: int,
    *,
    primary: bool = False,
    optional: bool = False,
) -> ModelFile:
    return ModelFile(name=name, sha256=sha256, size=size, primary=primary, optional=optional)


# Both STT entries are English streaming zipformers, and the difference between
# them is far larger than their labels suggest. Word error, measured on the
# development machine (Intel i9, CPU only), over 40 AMI utterances recorded on a
# single distant microphone -- a room, several speakers, spontaneous speech,
# which is what this application is actually for -- and over 40 LibriSpeech
# test-other clips, which are read aloud into a close microphone:
#
#     model                       AMI far-field    clean read    15 dB noise   RTF
#     kroko-2025-08-06                  31.6 %         6.4 %         27.3 %   0.08
#     zipformer-en-2023-06-26           98.5 %         5.1 %         55.5 %   0.16
#     (zipformer-en-20M, removed)       98.8 %        24.3 %         84.0 %   0.08
#
# The 2023-06-26 model is the best of the two on clean close read speech and
# unusable on anything else: on the far-field set it returned *nothing at all*
# for most utterances, and gain-normalising the audio only got it to 55%. That
# was this project's default until a real session showed the shape of it --
# "repeat back to me" heard as "THE PATE BACK TO ME", "gallon" as "GALLUM",
# "something went wrong" as "SOMETHING LENT WARM" -- while Deepgram, given the
# same audio, heard all three correctly. It was trained on read speech and it
# behaves like it. It stays registered because a headset and a quiet room are a
# real way to use this, and there it is the more accurate of the two.
#
# The 20M model is gone: it was slower than either of these on this machine and
# much worse at every condition measured, so it was 44 MB in every install for a
# model there was never a reason to recommend.
#
# Kroko is trained on a much larger and more varied corpus of real recordings,
# and it is the default for that reason. Two further consequences are load
# bearing rather than cosmetic: it punctuates and capitalises its output, which
# gives the turn heuristic a real end-of-thought signal (see local_stt.py), and
# its encoder names its own architecture in the ONNX metadata, so unlike the
# 2023-06-26 model it is built with no ``model_type``.
_LOCAL_STT_KROKO = ModelAsset(
    key="streaming-zipformer-en-kroko-2025-08-06",
    kind="stt",
    label="Zipformer streaming English, Kroko (~57 MB, punctuated, most accurate)",
    archive="sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06.tar.bz2",
    release=_STT_RELEASE,
    strip_prefix="sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06",
    files=(
        _f(
            "encoder.onnx",
            "d4881c57449d581e0770fd53fa66c2fdc6cd167d92ece7c715e603defc96d9d4",
            70092599,
        ),
        _f(
            "decoder.onnx",
            "455ba38466fce8d5a57e7db68a323b684079ca4d9e1dd93a740d9b2429aae3b1",
            617488,
        ),
        _f(
            "joiner.onnx",
            "d406f616736350e2a7df3e39398b78eb2fc1a2ca6973a19d3853fa3227e25b52",
            336817,
        ),
        _f(
            "tokens.txt",
            "396dbeb5f4858875690716084f54e90d339679d0ba3e6b5b584f3d7589254d2d",
            6310,
            primary=True,
        ),
    ),
    notes=(
        "Community model from Kroko ASR, CC-BY-SA per its model card "
        "(huggingface.co/Banafo/Kroko-ASR). Downloaded from the sherpa-onnx "
        "release at the user's request; not redistributed here. Its encoder "
        "declares its own architecture in the ONNX metadata, so no model_type "
        "is passed to the engine."
    ),
)

_LOCAL_STT_312M = ModelAsset(
    key="streaming-zipformer-en-2023-06-26",
    kind="stt",
    label="Zipformer streaming English, large (~71 MB, best on close, clear speech)",
    archive="sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2",
    release=_STT_RELEASE,
    strip_prefix="sherpa-onnx-streaming-zipformer-en-2023-06-26",
    files=(
        _f(
            "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
            "5022b2eca5b19d1bc104fcf33e26bc32604b7df553cd2e1f62e31dc7b05e9c87",
            70108816,
        ),
        _f(
            "decoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
            "780c63ee94c7cfa314211172e5d09b406c0da2beab5c40ea2f54cc95670b76a5",
            540688,
        ),
        _f(
            "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
            "abd5e30f3f16fc510605c6029dba33f10e4386bd75c5bdc30cf94076864db10d",
            259416,
        ),
        _f(
            "tokens.txt",
            "49e3c2646595fd907228b3c6787069658f67b17377c60aeb8619c4551b2316fb",
            5048,
            primary=True,
        ),
        # This archive also carries a 260 MB fp32 encoder that is never used.
        # Marking it optional means the installer *decompresses past it* instead
        # of writing and hashing it: unpacking it took minutes on Windows, where
        # tarfile has no system bz2, for a file that was then deleted.
        _f(
            "encoder-epoch-99-avg-1-chunk-16-left-128.onnx",
            "d1d74ba2fd9ce2186662cec93eab9f9c102d97a18e86dafdaf584776c961ab1d",
            260642850,
            optional=True,
        ),
    ),
    notes=(
        "The fp32 encoder is 260 MB and is deliberately not downloaded: the int8 "
        "one is pinned instead, and the fp32 member is an alternative the engine "
        "can fall back to only if it is already present. Kept as an option because "
        "it is the more accurate of the two on close, clearly spoken read speech "
        "-- and only there; see the table above before recommending it."
    ),
)

# The espeak-ng data below is what an English Piper voice needs to phonemise.
# It is pinned rather than trusted because inference does *not* verify it: a
# corrupt dictionary silently changes pronunciation instead of failing.
_LOCAL_TTS_PIPER = ModelAsset(
    key="vits-piper-en_US-lessac-medium",
    kind="tts",
    label="Piper en_US lessac medium (~19 MB, 22050 Hz)",
    archive="vits-piper-en_US-lessac-medium-int8.tar.bz2",
    release=_TTS_RELEASE,
    strip_prefix="vits-piper-en_US-lessac-medium-int8",
    files=(
        _f(
            "en_US-lessac-medium.onnx",
            "96a843df9c4da007e0fc224816cc6020fdc3482b47cb2684b8aab11fec8385ca",
            18579713,
            primary=True,
        ),
        _f(
            "tokens.txt",
            "87c8ef66eae5473ed0cc0366b3964c736ca6c5f676c979522ea31234e47430b9",
            921,
            primary=True,
        ),
        # The same file the upstream piper loader reads; it is what names the
        # espeak voice ("en-us"), and without it the model cannot be loaded at
        # all. sherpa-onnx itself does not read it, so it must be pinned here.
        _f(
            "en_US-lessac-medium.onnx.json",
            "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0",
            4885,
        ),
        _f(
            "espeak-ng-data/en_dict",
            "71bd330ba8a2e3e8076e631508208ef49449d6147c17b7bd2b4b1e1468292e35",
            166944,
        ),
        _f(
            "espeak-ng-data/phondata",
            "4e0288957874029a8c3c9f41a8f517ad4bf18127046decbdd4b9d1d6807ce3a3",
            550424,
        ),
        # These four are required, not optional: espeak-ng prints "Error
        # processing file .../phontab" and produces no audio — without raising —
        # if any is missing. Verified by pruning the data directory to each subset
        # and loading the voice.
        _f(
            "espeak-ng-data/phontab",
            "886f3fa402cb0ba73d483aa8ad000af47a6b7cc06293c75a97913fba68a530f6",
            55796,
        ),
        _f(
            "espeak-ng-data/phonindex",
            "3ca7b8fa3b42624e4b0f152707e7a39245fce569aa99ea47c055d9e622fcf0c4",
            39074,
        ),
        _f(
            "espeak-ng-data/intonations",
            "3f8af65fd3eda9759a10f021d61361c120871f463515229c925995c7f90918cc",
            2040,
        ),
        # The voice definition that maps "en-us" to a phoneme set...
        _f(
            "espeak-ng-data/lang/gmw/en-US",
            "41534c2a22df5dd4f1052ff9e1a33a3ea7bff5a26b5c02bdad5ba8ddb7524704",
            257,
        ),
        # ...and its parent language entry, which espeak-ng reads on the way.
        _f(
            "espeak-ng-data/lang/gmw/en",
            "4605d5330801de3641c6e366d15f129ea1f5ffbce8722642aba01ace07ab9c83",
            140,
        ),
    ),
    requires_dirs=("espeak-ng-data", "espeak-ng-data/lang", "espeak-ng-data/voices"),
    notes="English phonemisation is espeak-ng based; its data ships in the archive.",
)

# Order is the recommendation: `registry_key` falls back to the first entry of a
# kind when nothing is configured, so the first STT entry here is the default.
MODEL_REGISTRY: dict[str, ModelAsset] = {
    asset.key: asset for asset in (_LOCAL_STT_KROKO, _LOCAL_STT_312M, _LOCAL_TTS_PIPER)
}


def model_keys(kind: str | None = None) -> tuple[str, ...]:
    """Registered model keys, optionally filtered by kind."""
    return tuple(a.key for a in iter_assets(kind))


def registry_key(*, kind: str, name: str | None) -> str:
    """Resolve a configured model name to a registry key.

    An unknown name is reported rather than silently substituted: a user who
    typed a model name that does not exist should be told, not have a different
    voice chosen for them.
    """
    available = model_keys(kind)
    if name:
        if name in available:
            return name
        raise ModelUnavailable(
            f"no local {kind} model named {name!r}",
            fix=f"Available {kind} models: {', '.join(available)}.",
        )
    if not available:
        raise ModelUnavailable(f"no local {kind} models are registered")
    # The registry is ordered best-first inside each kind, so the first entry is
    # the recommended default.
    return available[0]


def iter_assets(kind: str | None = None) -> Iterator[ModelAsset]:
    """Iterate the registry, optionally filtered by kind."""
    for asset in MODEL_REGISTRY.values():
        if kind is None or asset.kind == kind:
            yield asset


def models_dir(settings: Settings) -> Path:
    """Directory local models live in."""
    return settings.models_path


def model_root(settings: Settings, asset: ModelAsset) -> Path:
    """Where one model's files live."""
    return models_dir(settings) / asset.key


def missing_files(asset: ModelAsset, root: Path) -> list[ModelFile]:
    """Required files that are absent or the wrong size.

    Optional members are not required: a model that ships a large alternative
    variant is still installed without it, and reporting it as missing would make
    every install look broken and every repair re-download 260 MB nobody wanted.
    """
    missing: list[ModelFile] = []
    for entry in asset.files:
        if entry.optional:
            continue
        path = root / entry.name
        try:
            if path.stat().st_size != entry.size:
                missing.append(entry)
        except OSError:
            missing.append(entry)
    for directory in asset.requires_dirs:
        if not (root / directory).is_dir():
            missing.append(ModelFile(name=directory, sha256="", size=0))
    return missing


@dataclass(slots=True)
class ModelStatus:
    """What the UI and ``doctor`` need to know about one model.

    ``missing`` is every file that is not correct, which covers both "never
    installed" and "installed but damaged". ``partial`` distinguishes them, and it
    is the distinction that matters for repair: a model nobody chose to install is
    not a problem, while a half-written one is.
    """

    key: str
    kind: str
    label: str
    present: bool
    root: Path
    missing: list[str] = field(default_factory=list)
    total_bytes: int = 0
    # True when some required files are on disk and others are not, or when a file
    # is the wrong size or fails its checksum.
    partial: bool = False

    @property
    def summary(self) -> str:
        if self.present:
            return f"{self.label} — installed"
        if self.partial:
            shown = ", ".join(self.missing[:3])
            more = "" if len(self.missing) <= 3 else f" (+{len(self.missing) - 3} more)"
            return f"{self.label} — damaged, reinstall ({shown}{more})"
        return f"{self.label} — not installed"

    @property
    def fix(self) -> str | None:
        if self.present:
            return None
        return f"Run `surtitle models download {self.key}`."


def _status_for(asset: ModelAsset, root: Path) -> ModelStatus:
    """Compute one model's status, including whether it is partly present."""
    missing = missing_files(asset, root)
    on_disk = sum(1 for entry in asset.files if (root / entry.name).exists())
    return ModelStatus(
        key=asset.key,
        kind=asset.kind,
        label=asset.label,
        present=not missing,
        root=root,
        missing=[entry.name for entry in missing],
        total_bytes=asset.total_bytes,
        partial=bool(missing) and on_disk > 0,
    )


def status(settings: Settings, *, kind: str | None = None) -> list[ModelStatus]:
    """Report every registered model, or only one kind."""
    return [_status_for(asset, model_root(settings, asset)) for asset in iter_assets(kind)]


def describe(settings: Settings) -> list[dict[str, Any]]:
    """JSON-friendly model report, for the Settings screen and the API."""
    return [
        {
            "key": item.key,
            "kind": item.kind,
            "label": item.label,
            "present": item.present,
            "missing": item.missing,
            "bytes": item.total_bytes,
            "path": str(item.root),
            "fix": item.fix,
        }
        for item in status(settings)
    ]


def _asset(kind: str, name: str | None) -> ModelAsset:
    return MODEL_REGISTRY[registry_key(kind=kind, name=name)]


def _require(asset: ModelAsset, root: Path) -> None:
    absent = missing_files(asset, root)
    if absent:
        names = ", ".join(entry.name for entry in absent if entry.name)
        raise ModelUnavailable(
            f"local {asset.kind} model {asset.key} is not installed ({names})",
            fix=f"Run `surtitle models download {asset.key}`.",
        )


def resolve_stt(settings: Settings) -> dict[str, str]:
    """Paths a streaming recogniser needs, for the configured STT model.

    Raises :class:`ModelUnavailable` naming exactly which file is missing, so a
    broken install reports a filename instead of a dead microphone.
    """
    asset = _asset("stt", settings.local_stt_model)
    root = model_root(settings, asset)
    _require(asset, root)

    # Both registered models pin the int8 encoder, because the fp32 encoders are
    # 89 MB and 260 MB. The fp32 preference is honoured when a model that ships
    # one is registered later, and is otherwise a no-op rather than an error.
    int8 = [entry for entry in asset.files if entry.name.endswith(".int8.onnx")]
    fp32 = [entry for entry in asset.files if not entry.name.endswith(".int8.onnx")]

    def pick(group: list[ModelFile], stem: str) -> str | None:
        for entry in group:
            if stem in entry.name:
                return str(root / entry.name)
        return None

    encoder = pick(fp32, "encoder") if not settings.local_stt_int8 else None
    if encoder is None:
        encoder = pick(int8, "encoder") or pick(fp32, "encoder")
    decoder = pick(int8, "decoder") or pick(fp32, "decoder")
    joiner = pick(int8, "joiner") or pick(fp32, "joiner")
    tokens = asset.find("tokens.txt")
    if encoder is None or decoder is None or joiner is None or tokens is None:
        raise ModelUnavailable(
            f"the {asset.key} archive does not contain the expected files",
            fix="Delete the model directory and download it again.",
        )
    return {
        "encoder": encoder,
        "decoder": decoder,
        "joiner": joiner,
        "tokens": str(root / tokens.name),
        # The two model families do not share an architecture and the wrong value
        # fails at load, so it is derived from the files rather than assumed.
        "model_type": "zipformer2" if "chunk-16" in (decoder or "") else "",
    }


def resolve_tts(settings: Settings) -> dict[str, str]:
    """Paths a synthesiser needs, for the configured TTS model."""
    asset = _asset("tts", settings.local_tts_model)
    root = model_root(settings, asset)
    _require(asset, root)
    model = asset.find("en_US-lessac-medium.onnx") or asset.find("model.onnx")
    if model is None:
        for entry in asset.files:
            if entry.name.endswith(".onnx"):
                model = entry
                break
    tokens = asset.find("tokens.txt")
    if model is None or tokens is None:
        raise ModelUnavailable(
            f"the {asset.key} archive does not contain the expected files",
            fix="Delete the model directory and download it again.",
        )
    return {
        "model": str(root / model.name),
        "tokens": str(root / tokens.name),
        # Piper voices are phonemised with espeak-ng, whose data ships inside the
        # archive. Passed explicitly rather than guessed, because a missing
        # dictionary changes pronunciation silently instead of failing.
        "data_dir": str(root / "espeak-ng-data"),
    }


# --- downloading ---------------------------------------------------------


@dataclass(slots=True)
class Progress:
    """One update from :func:`download`, for a CLI bar or a UI notice."""

    asset: str
    stage: str  # "download" | "extract" | "verify"
    received: int = 0
    total: int = 0
    message: str = ""


ProgressHandler = Callable[[Progress], None]


def download(
    settings: Settings,
    *,
    kind: str | None = None,
    keys: tuple[str, ...] = (),
    progress: ProgressHandler | None = None,
    force: bool = False,
) -> list[ModelStatus]:
    """Download and install missing models, then report their status.

    ``keys`` and ``kind`` select what to fetch; with neither, every registered
    model is considered. Installed models are left alone unless ``force`` is
    set: re-downloading 85 MB to fix a problem that is not the download wastes
    the user's connection.
    """
    if keys:
        unknown = [k for k in keys if k not in MODEL_REGISTRY]
        if unknown:
            raise ModelUnavailable(
                f"unknown model(s): {', '.join(unknown)}",
                fix=f"Available models: {', '.join(model_keys())}.",
            )
        targets = [MODEL_REGISTRY[k] for k in keys]
    else:
        targets = list(iter_assets(kind))

    for asset in targets:
        destination = model_root(settings, asset)
        if not force and not missing_files(asset, destination):
            _emit(progress, asset.key, "verify", message="already installed")
            continue
        _download_asset(asset, destination, progress=progress)

    return status(settings, kind=kind)


def _emit(
    progress: ProgressHandler | None,
    asset: str,
    stage: str,
    received: int = 0,
    total: int = 0,
    message: str = "",
) -> None:
    if progress is not None:
        progress(
            Progress(asset=asset, stage=stage, received=received, total=total, message=message)
        )


def _download_asset(
    asset: ModelAsset,
    destination: Path,
    *,
    progress: ProgressHandler | None,
    attempts: int = 3,
) -> None:
    """Fetch one archive, extract the required files, and verify them.

    Everything happens inside a private staging directory that is renamed into
    place at the end, so the next start either sees a complete model or sees
    nothing at all.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{asset.key}-", dir=str(destination.parent)))
    try:
        archive = staging / asset.archive
        _fetch(asset, archive, progress=progress)

        _emit(progress, asset.key, "extract", message="extracting")

        # Trim the archive to the files that are actually needed, reading only
        # those members. The Piper archive carries 19 MB of espeak data for every
        # language in the world; an English voice needs one dictionary and the
        # shared tables. The large zipformer archive carries a 260 MB fp32 encoder
        # that is not used at all — unpacking it would mean writing and hashing a
        # quarter of a gigabyte to then delete it, which took minutes on Windows.
        staged = staging / "model"
        staged.mkdir()
        _extract_required(asset, archive, staged)
        for directory in asset.requires_dirs:
            (staged / directory).mkdir(parents=True, exist_ok=True)

        _verify_staged(asset, staged, progress=progress)

        if destination.exists():
            shutil.rmtree(destination)
        try:
            staged.rename(destination)
        except OSError:
            # A cross-device rename fails; copy instead of leaving nothing.
            shutil.copytree(staged, destination)
        _emit(progress, asset.key, "verify", message="installed")
    except ModelUnavailable:
        raise
    except (urllib.error.URLError, OSError, tarfile.TarError) as exc:
        if attempts > 1:
            _emit(progress, asset.key, "download", message=f"retrying after {exc}")
            _download_asset(asset, destination, progress=progress, attempts=attempts - 1)
            return
        raise ModelUnavailable(
            f"could not install {asset.key}: {exc}",
            fix=f"Check your connection, then run `surtitle models download {asset.key}`.",
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _fetch(asset: ModelAsset, target: Path, *, progress: ProgressHandler | None) -> None:
    """Download the first URL that answers, streaming to ``target``."""
    errors: list[str] = []
    partial = target.with_name(target.name + ".part")
    for url in asset.urls:
        try:
            with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as response:
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                with partial.open("wb") as handle:
                    while chunk := response.read(_CHUNK):
                        handle.write(chunk)
                        received += len(chunk)
                        _emit(progress, asset.key, "download", received, total)
            if total and partial.stat().st_size != total:
                raise OSError(f"short download: {partial.stat().st_size} of {total} bytes")
            partial.replace(target)
            return
        except (urllib.error.URLError, OSError) as exc:
            errors.append(f"{url}: {exc}")
            partial.unlink(missing_ok=True)
    raise ModelUnavailable(
        f"no download source worked for {asset.key}",
        fix="Last error: " + (errors[-1] if errors else "unknown"),
    )


def _extract_required(asset: ModelAsset, archive: Path, destination: Path) -> None:
    """Extract only the archive members this model needs.

    Reading members one at a time and stopping early is what keeps the install
    affordable: ``extractall`` writes every member, so the large zipformer model
    would unpack 260 MB of fp32 encoder that is immediately deleted, and the
    Piper voice would unpack 19 MB of dictionaries for languages it cannot speak.
    On Windows this is the difference between a few seconds and several minutes,
    because ``tarfile`` has no system ``libbz2`` to lean on there.

    Members the model declares but does not need (``optional``) are skipped
    without being written: the bz2 stream is read past them, which costs
    decompression but no disk and no hashing.
    """
    wanted: dict[str, ModelFile] = {entry.name: entry for entry in asset.files}
    root = destination.resolve()
    written: set[str] = set()

    with tarfile.open(archive, "r:bz2") as bundle:
        for member in bundle:
            name = member.name
            # Accept the archive's own layout, whether or not it is nested under
            # a directory, so a repackaged mirror still installs.
            relative = name
            if asset.strip_prefix and name.startswith(f"{asset.strip_prefix}/"):
                relative = name[len(asset.strip_prefix) + 1 :]
            entry = wanted.get(relative)
            if entry is None or entry.optional:
                continue
            if member.issym() or member.islnk():
                continue  # a second way out of the tree, and never needed here
            target = (root / relative).resolve()
            if target != root and root not in target.parents:
                raise ModelUnavailable(
                    f"{archive.name} contains an unsafe path ({member.name})",
                    fix="Report this; the archive is not the one we expect.",
                )
            extracted = bundle.extractfile(member)
            if extracted is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with extracted, target.open("wb") as handle:
                shutil.copyfileobj(extracted, handle, 1 << 20)
            written.add(relative)
            # Nothing else is needed, so stop decompressing.
            if written >= {entry.name for entry in asset.files if not entry.optional}:
                break


def _safe_extract(archive: Path, destination: Path) -> None:
    """Extract an entire tar archive, refusing members that escape ``destination``.

    Kept for callers that genuinely want everything, and used by the tests that
    pin the path-traversal guard. The installer itself uses
    :func:`_extract_required`, which reads only the members it needs.
    """
    root = destination.resolve()
    with tarfile.open(archive, "r:bz2") as bundle:
        for member in bundle.getmembers():
            if member.issym() or member.islnk():
                continue  # a second way out of the tree, and never needed here
            target = (root / member.name).resolve()
            if target != root and root not in target.parents:
                raise ModelUnavailable(
                    f"{archive.name} contains an unsafe path ({member.name})",
                    fix="Report this; the archive is not the one we expect.",
                )
        try:
            bundle.extractall(destination, filter="data")
        except TypeError:  # pragma: no cover - Python < 3.12 without the filter
            bundle.extractall(destination)


def _verify_staged(asset: ModelAsset, staged: Path, *, progress: ProgressHandler | None) -> None:
    """Check every pinned file before it is installed.

    Optional members are checked only if they were written: they are skipped by
    :func:`_extract_required`, so demanding them here would fail every install.
    """
    for entry in asset.files:
        path = staged / entry.name
        if not path.is_file():
            if entry.optional:
                continue
            raise ModelUnavailable(
                f"{asset.archive} did not provide {entry.name}",
                fix="Try the download again; a mirror may be incomplete.",
            )
        size = path.stat().st_size
        if size != entry.size:
            raise ModelUnavailable(
                f"{entry.name} is the wrong size ({size} bytes, expected {entry.size})",
                fix="The download was truncated; run the download again.",
            )
        _emit(progress, asset.key, "verify", 0, entry.size, f"verifying {entry.name}")
        digest = _sha256(path)
        if digest != entry.sha256:
            raise ModelUnavailable(
                f"{entry.name} failed its checksum",
                fix="Delete the model directory and download it again.",
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify(settings: Settings, *, kind: str | None = None) -> list[ModelStatus]:
    """Re-check checksums for installed models, reporting corruption.

    This is the support path for "it worked yesterday": a half-written file that
    happens to have the right size is the one failure a size check cannot catch.
    """
    report: list[ModelStatus] = []
    for asset in iter_assets(kind):
        root = model_root(settings, asset)
        item = _status_for(asset, root)
        if not item.missing:
            broken = [
                entry.name
                for entry in asset.files
                if (root / entry.name).is_file() and _sha256(root / entry.name) != entry.sha256
            ]
            if broken:
                item.missing = broken
                item.present = False
                item.partial = True
        report.append(item)
    return report
