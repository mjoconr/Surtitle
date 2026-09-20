"""Tests for the local speech engine selection and the model manager.

Everything here is offline by construction. The model files are 43-71 MB downloads
and no test may reach the network, so:

* ``sherpa_onnx`` is never imported: the local engines import it inside
  ``start()``/``_load()`` precisely so that these tests can run without the extra.
* Model installation is exercised against a real tar archive built in a temp
  directory, served over a ``file://`` URL — the same code path as the network,
  with none of the network.

The checksum and atomicity behaviour is what these tests exist for: a truncated
ONNX file does not raise, it produces garbage transcripts, and an interrupted
install must not be mistaken for a complete one.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path

import pytest

from surtitle.config import Settings
from surtitle.voice import models
from surtitle.voice.models import ModelAsset, ModelFile, ModelUnavailable

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "DEEPSEEK_API_KEY": "k",
        "SURTITLE_MODELS_DIR": str(tmp_path / "models"),
    }
    values.update(overrides)
    return Settings(**values)


def place_files(settings: Settings, asset: ModelAsset, *, skip_optional: bool = False) -> None:
    """Create an asset's files at exactly their pinned size, sparsely.

    ``truncate`` rather than writing the bytes: one of the archives carries a
    260 MB encoder, and a test has no business writing a quarter of a gigabyte to
    check that a path resolves.
    """
    root = models.model_root(settings, asset)
    for entry in asset.files:
        if skip_optional and entry.optional:
            continue
        target = root / entry.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("r+b" if target.exists() else "wb") as handle:
            handle.truncate(entry.size)
    for directory in asset.requires_dirs:
        (root / directory).mkdir(parents=True, exist_ok=True)


def tiny_asset(archive: str, *, prefix: str = "model") -> ModelAsset:
    """A one-file asset whose checksum is computed from the real content."""
    content = b"pretend this is an ONNX file"
    import hashlib

    return ModelAsset(
        key="tiny",
        kind="stt",
        label="Tiny test model",
        archive=archive,
        release="asr-models",
        strip_prefix=prefix,
        files=(
            ModelFile(
                name="encoder.onnx",
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
                primary=True,
            ),
        ),
    )


def build_archive(path: Path, *, prefix: str = "model", members: dict[str, bytes] | None = None):
    """Write a tar.bz2 containing ``prefix/<file>`` entries."""
    payload = {"encoder.onnx": b"pretend this is an ONNX file"}
    if members:
        payload.update(members)
    with tarfile.open(path, "w:bz2") as bundle:
        for name, data in payload.items():
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


class TestRegistryIntegrity:
    """A wrong checksum or a missing URL is a broken install no test could catch
    later, so the table itself is asserted."""

    def test_every_model_has_urls_and_pinned_files(self):
        for asset in models.iter_assets():
            assert asset.urls, f"{asset.key} has no download URL"
            assert asset.urls[0].startswith("https://"), asset.key
            assert asset.files, f"{asset.key} has no required files"
            for entry in asset.files:
                assert entry.size > 0, f"{asset.key}/{entry.name} has no pinned size"
                assert len(entry.sha256) == 64, f"{asset.key}/{entry.name} checksum is not sha256"
                assert all(c in "0123456789abcdef" for c in entry.sha256), entry.name

    def test_every_kind_has_at_least_one_model(self):
        assert models.model_keys("stt")
        assert models.model_keys("tts")

    def test_defaults_in_config_are_registered(self):
        settings = Settings(DEEPSEEK_API_KEY="k")
        assert settings.local_stt_model in models.MODEL_REGISTRY
        assert settings.local_tts_model in models.MODEL_REGISTRY
        assert models.MODEL_REGISTRY[settings.local_stt_model].kind == "stt"
        assert models.MODEL_REGISTRY[settings.local_tts_model].kind == "tts"

    def test_the_default_is_the_recommended_model(self):
        """The config default and the registry's first entry have to agree.

        `registry_key` reads the first entry of a kind as the recommendation, so a
        default named separately in :mod:`surtitle.config` can drift away from it
        -- leaving a model that is registered but never chosen and a default that
        is never recommended.
        """
        settings = Settings(DEEPSEEK_API_KEY="k")
        assert models.registry_key(kind="stt", name=None) == settings.local_stt_model
        assert models.registry_key(kind="tts", name=None) == settings.local_tts_model

    def test_the_quoted_size_leaves_out_what_is_never_downloaded(self):
        """The install prompt asks for consent with a real figure.

        One archive carries a 260 MB encoder variant that the installer skips, so
        counting it would have overstated that model's download by a factor of
        four -- in the one place the project promises a number the download will
        keep.
        """
        for asset in models.iter_assets():
            assert asset.total_bytes == sum(f.size for f in asset.files if not f.optional)
        large = models.MODEL_REGISTRY["streaming-zipformer-en-2023-06-26"]
        assert [f for f in large.files if f.optional], "this test needs an optional member"
        assert large.total_bytes < max(f.size for f in large.files), (
            "the optional fp32 encoder is still being counted"
        )

    def test_unknown_model_name_is_reported_not_substituted(self):
        """A typo must not silently install a different voice."""
        with pytest.raises(ModelUnavailable) as caught:
            models.registry_key(kind="stt", name="not-a-model")
        assert "not-a-model" in str(caught.value)
        assert caught.value.fix


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_empty_cache_reports_everything_missing(self, tmp_path):
        settings = make_settings(tmp_path)
        report = models.status(settings)
        assert report and all(not item.present for item in report)
        assert all(not item.partial for item in report), "nothing on disk is not damage"

    def test_partially_installed_model_is_reported_as_damage(self, tmp_path):
        """A model with some files present is the case worth repairing."""
        settings = make_settings(tmp_path)
        asset = models.MODEL_REGISTRY[settings.local_tts_model]
        root = models.model_root(settings, asset)
        root.mkdir(parents=True)
        # One required file, correct; the rest absent.
        entry = asset.files[0]
        (root / entry.name).write_bytes(b"x" * entry.size)

        item = models.status(settings, kind="tts")[0]
        assert not item.present
        assert item.partial, "a partly written model must not read as 'not installed'"
        assert entry.name not in item.missing

    def test_wrong_size_counts_as_missing(self, tmp_path):
        settings = make_settings(tmp_path)
        asset = models.MODEL_REGISTRY[settings.local_tts_model]
        root = models.model_root(settings, asset)
        root.mkdir(parents=True)
        for entry in asset.files:
            target = root / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"short")
        item = models.status(settings, kind="tts")[0]
        assert not item.present
        # Every pinned file is reported, which is what makes the repair command
        # able to name what is wrong.
        assert set(item.missing) >= {entry.name for entry in asset.files}

    def test_describe_is_json_friendly(self, tmp_path):
        described = models.describe(make_settings(tmp_path))
        assert all(isinstance(row, dict) for row in described)
        assert {"key", "kind", "label", "present", "missing", "bytes", "path"} <= set(described[0])


# ---------------------------------------------------------------------------
# resolve_*
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_reports_the_missing_file_by_name(self, tmp_path):
        settings = make_settings(tmp_path)
        with pytest.raises(ModelUnavailable) as caught:
            models.resolve_stt(settings)
        message = str(caught.value)
        assert settings.local_stt_model in message
        assert "not installed" in message
        assert caught.value.fix and "models download" in caught.value.fix

    def test_resolve_returns_the_paths_the_engine_needs(self, tmp_path):
        """Every registered model resolves, whatever its files happen to be called.

        The STT entries do not share a naming convention -- one pins
        ``encoder-….int8.onnx``, another plain ``encoder.onnx`` -- so resolution has
        to be driven by the registry rather than by a name that only one of them
        uses.
        """
        settings = make_settings(tmp_path)
        for kind, resolver, attribute in (
            ("stt", models.resolve_stt, "local_stt_model"),
            ("tts", models.resolve_tts, "local_tts_model"),
        ):
            for asset in models.iter_assets(kind):
                setattr(settings, attribute, asset.key)
                place_files(settings, asset)

                resolved = resolver(settings)
                for key, path in resolved.items():
                    if key == "model_type":
                        continue
                    assert Path(path).is_file() or Path(path).is_dir(), (
                        f"{asset.key}: {key} is {path}"
                    )
                if kind == "stt":
                    for needed in ("encoder", "decoder", "joiner", "tokens"):
                        assert Path(resolved[needed]).is_file(), f"{asset.key}: {needed}"
                else:
                    assert resolved["data_dir"].endswith("espeak-ng-data")
                    assert Path(resolved["data_dir"]).is_dir()

    def test_the_int8_encoder_is_preferred_when_a_model_ships_both(self, tmp_path):
        """`local_stt_int8` only means anything for a model that has both."""
        settings = make_settings(tmp_path)
        asset = models.MODEL_REGISTRY["streaming-zipformer-en-2023-06-26"]
        settings.local_stt_model = asset.key
        place_files(settings, asset)

        int8 = models.resolve_stt(settings)
        assert int8["encoder"].endswith(".int8.onnx"), "int8 is the default"
        assert int8["model_type"] == "zipformer2"

        settings.local_stt_int8 = False
        fp32 = models.resolve_stt(settings)
        assert fp32["encoder"].endswith("chunk-16-left-128.onnx")
        assert not fp32["encoder"].endswith(".int8.onnx")

    def test_a_model_that_declares_its_own_architecture_passes_no_model_type(self, tmp_path):
        """Kroko's encoder carries the architecture in its ONNX metadata.

        sherpa-onnx reads `model_type` from the metadata when it is empty, so
        naming one here would be guessing at something the file already says.
        """
        settings = make_settings(tmp_path)
        settings.local_stt_model = "streaming-zipformer-en-kroko-2025-08-06"
        place_files(settings, models.MODEL_REGISTRY[settings.local_stt_model])

        assert models.resolve_stt(settings)["model_type"] == ""


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


class TestInstall:
    def _install(self, monkeypatch, tmp_path, *, corrupt: bool = False):
        """Install a tiny model from a real archive over a file:// URL."""
        settings = make_settings(tmp_path)
        source = tmp_path / "src"
        source.mkdir()
        archive = source / "tiny.tar.bz2"
        build_archive(archive)
        asset = tiny_asset(archive.name)
        monkeypatch.setitem(models.MODEL_REGISTRY, "tiny", asset)
        monkeypatch.setattr(
            ModelAsset,
            "urls",
            property(lambda self: (archive.as_uri(),)),
        )
        if corrupt:
            # Serve an archive whose extracted file will fail its checksum.
            build_archive(archive, members={"encoder.onnx": b"different bytes entirely"})
        return settings, asset, archive

    def test_download_installs_and_verifies(self, monkeypatch, tmp_path):
        settings, asset, _archive = self._install(monkeypatch, tmp_path)
        models.download(settings, keys=("tiny",))
        root = models.model_root(settings, asset)
        assert (root / "encoder.onnx").read_bytes() == b"pretend this is an ONNX file"
        assert models.status(settings)[-1].present

    def test_a_bad_checksum_is_refused_and_leaves_nothing_behind(self, monkeypatch, tmp_path):
        """The whole point of pinning a hash: garbage must not be installed."""
        settings, asset, archive = self._install(monkeypatch, tmp_path)
        # Serve a same-size archive whose bytes differ, so the failure has to be
        # the checksum rather than the length check. A truncated download is a
        # different (and much easier) failure.
        original = b"pretend this is an ONNX file"
        build_archive(archive, members={"encoder.onnx": b"X" * len(original)})
        with pytest.raises(ModelUnavailable) as caught:
            models.download(settings, keys=("tiny",))
        assert "checksum" in str(caught.value)
        root = models.model_root(settings, asset)
        assert not root.exists(), "a failed install must not leave a usable-looking model"
        leftovers = [p for p in (tmp_path / "models").iterdir() if p.name.startswith(".tiny")]
        assert not leftovers, "staging directories must be cleaned up"

    def test_a_short_download_is_detected(self, monkeypatch, tmp_path):
        settings, asset, archive = self._install(monkeypatch, tmp_path)
        # Claim a size the file does not have, which is what a dropped connection
        # looks like before any hashing happens.
        broken = tiny_asset(archive.name)
        monkeypatch.setitem(
            models.MODEL_REGISTRY,
            "tiny",
            ModelAsset(
                key=broken.key,
                kind=broken.kind,
                label=broken.label,
                archive=broken.archive,
                release=broken.release,
                strip_prefix=broken.strip_prefix,
                files=(
                    ModelFile(
                        name="encoder.onnx",
                        sha256=broken.files[0].sha256,
                        size=broken.files[0].size,
                    ),
                ),
            ),
        )
        # Truncate the served archive's declaration by pointing at a different one
        # whose member is smaller than the pinned size.
        build_archive(archive, members={"encoder.onnx": b"short"})
        with pytest.raises(ModelUnavailable):
            models.download(settings, keys=("tiny",))
        assert not models.model_root(settings, asset).exists()

    def test_already_installed_models_are_not_downloaded_again(self, monkeypatch, tmp_path):
        settings, _asset, _archive = self._install(monkeypatch, tmp_path)
        models.download(settings, keys=("tiny",))

        calls: list[str] = []

        def refuse(*_args, **_kwargs):  # pragma: no cover - must not run
            calls.append("fetched")
            raise AssertionError("an installed model must not be downloaded again")

        monkeypatch.setattr(models.urllib.request, "urlopen", refuse)
        models.download(settings, keys=("tiny",))
        assert not calls

    def test_unknown_key_is_rejected_before_any_download(self, tmp_path):
        settings = make_settings(tmp_path)
        with pytest.raises(ModelUnavailable) as caught:
            models.download(settings, keys=("nope",))
        assert "unknown model" in str(caught.value)

    def test_unsafe_archive_members_are_refused(self, tmp_path):
        """A tar member that escapes the extraction root is a write primitive."""
        archive = tmp_path / "evil.tar.bz2"
        with tarfile.open(archive, "w:bz2") as bundle:
            info = tarfile.TarInfo("../../escaped.txt")
            payload = b"pwned"
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))

        destination = tmp_path / "out"
        destination.mkdir()
        with pytest.raises(ModelUnavailable) as caught:
            models._safe_extract(archive, destination)
        assert "unsafe path" in str(caught.value)
        assert not (tmp_path.parent / "escaped.txt").exists()

    def test_extraction_refuses_a_symlink_out_of_the_tree(self, tmp_path):
        """A relative symlink out of the tree is a portable escape attempt.

        Which layer refuses it is not the point; that it is refused is. Both the
        explicit member check in ``_safe_extract`` and ``tarfile``'s own ``data``
        filter reject it, and a model archive never legitimately needs a link.
        """
        archive = tmp_path / "links.tar.bz2"
        with tarfile.open(archive, "w:bz2") as bundle:
            link = tarfile.TarInfo("model/encoder.onnx")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../escaped.txt"
            bundle.addfile(link)
        destination = tmp_path / "out"
        destination.mkdir()
        with pytest.raises((ModelUnavailable, tarfile.TarError)):
            models._safe_extract(archive, destination)
        assert not (destination / "model" / "encoder.onnx").exists()
        assert not (tmp_path / "escaped.txt").exists()

    def test_only_the_required_members_are_written(self, tmp_path):
        """The installer must not unpack what it is going to delete.

        The large zipformer archive ships a 260 MB fp32 encoder that is never
        used. ``extractall`` wrote it and then hashed it — minutes of pointless
        work on Windows, where tarfile has no system bz2 to lean on — so
        extraction now reads only the members the model declares.
        """
        import hashlib

        archive = tmp_path / "model.tar.bz2"
        required = b"the encoder we want"
        # An optional member *and* an undeclared one: neither may be written.
        with tarfile.open(archive, "w:bz2") as bundle:
            for name, payload in (
                ("model/encoder.onnx", required),
                ("model/encoder.fp32.onnx", b"x" * 4096),
                ("model/README.md", b"notes"),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))

        asset = ModelAsset(
            key="tiny",
            kind="stt",
            label="tiny",
            archive=archive.name,
            release="asr-models",
            strip_prefix="model",
            files=(
                ModelFile(
                    name="encoder.onnx",
                    sha256=hashlib.sha256(required).hexdigest(),
                    size=len(required),
                    primary=True,
                ),
                ModelFile(
                    name="encoder.fp32.onnx",
                    sha256=hashlib.sha256(b"x" * 4096).hexdigest(),
                    size=4096,
                    optional=True,
                ),
            ),
        )

        destination = tmp_path / "out"
        destination.mkdir()
        models._extract_required(asset, archive, destination)

        assert (destination / "encoder.onnx").read_bytes() == required
        assert not (destination / "encoder.fp32.onnx").exists(), (
            "an optional member must be skipped, not written and then deleted"
        )
        assert not (destination / "README.md").exists(), (
            "an undeclared member must not be written at all"
        )
        # The skipped member must also not be demanded by verification.
        models._verify_staged(asset, destination, progress=None)

    def test_an_optional_member_is_not_reported_as_missing(self, tmp_path):
        settings = make_settings(tmp_path)
        # Named rather than taken from the default: only one of the registered
        # models ships a variant it does not want, and that is the case under test.
        asset = models.MODEL_REGISTRY["streaming-zipformer-en-2023-06-26"]
        assert [entry for entry in asset.files if entry.optional], (
            "the large STT model is expected to mark the fp32 encoder optional"
        )
        place_files(settings, asset, skip_optional=True)

        assert models.missing_files(asset, models.model_root(settings, asset)) == [], (
            "a model without its optional file must still count as installed"
        )


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


class TestVerify:
    def test_corrupt_content_of_the_right_size_is_caught(self, tmp_path):
        """The one failure a size check cannot see."""
        settings = make_settings(tmp_path)
        asset = models.MODEL_REGISTRY[settings.local_tts_model]
        root = models.model_root(settings, asset)
        for entry in asset.files:
            target = root / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"z" * entry.size)
        for directory in asset.requires_dirs:
            (root / directory).mkdir(parents=True, exist_ok=True)

        plain = models.status(settings, kind="tts")[0]
        assert plain.present, "status only checks sizes, so this looks installed"

        checked = models.verify(settings, kind="tts")[0]
        assert not checked.present
        assert checked.partial
        assert len(checked.missing) == len(asset.files)

    def test_absent_models_are_not_reported_as_damage(self, tmp_path):
        rows = models.verify(make_settings(tmp_path))
        assert all(not item.partial for item in rows)


# ---------------------------------------------------------------------------
# Disk layout
# ---------------------------------------------------------------------------


class TestModelsDir:
    def test_default_is_under_the_data_dir(self, tmp_path):
        settings = Settings(DEEPSEEK_API_KEY="k", SURTITLE_HOME=str(tmp_path / "home"))
        assert models.models_dir(settings) == tmp_path / "home" / "models"

    def test_override_is_honoured(self, tmp_path):
        settings = Settings(DEEPSEEK_API_KEY="k", SURTITLE_MODELS_DIR=str(tmp_path / "elsewhere"))
        assert models.models_dir(settings) == tmp_path / "elsewhere"


def test_cleanup_helper_used_by_tests_does_not_leak(tmp_path):
    """Guard: the fixtures above must not write outside their tmp_path."""
    marker = tmp_path / "marker"
    marker.write_text("ok", encoding="utf-8")
    assert marker.exists()
    shutil.rmtree(tmp_path, ignore_errors=True)
