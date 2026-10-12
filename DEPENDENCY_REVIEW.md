# Dependency and diarization review — 2026-10-12

Work started from clean `main` at `d5d3d15`. No audit text or advisory IDs were
provided, so findings were verified against the implementation, installed
distribution sources/metadata, `uv.lock`, and current upstream advisories.
All application changes are local and uncommitted. After explicit user approval,
the dependency patch was applied to [pyproject.toml](pyproject.toml) and
[uv.lock](uv.lock), and exactly the five approved security updates were installed.

## Advisory findings

Before updating, queried OSV's public `v1/querybatch` API for all **147 registry
package/version entries** in `uv.lock` (including optional and platform-specific
dependencies; the editable application is excluded). Eleven returned records
reduced to six distinct advisories affecting five distributions after alias
deduplication. Repeating the scan after the updates returned **zero findings
across all 147 entries** on 2026-10-12.
Cross-checked the findings with upstream advisories or release notes. This is
a version inventory, not proof that each vulnerable path is reachable here;
database coverage and future disclosures remain limitations.

| Previously locked distribution | Verified advisory | Smallest patched version | Relevant conditions |
| --- | --- | --- | --- |
| urllib3 2.7.0 | [GHSA-8988-9cw3-xx77](https://github.com/urllib3/urllib3/security/advisories/GHSA-8988-9cw3-xx77), CVE-2026-97687 | 2.8.0 | HTTPS proxy and destination TLS settings can interfere. Affects >=1.26.0,<2.8.0. |
| urllib3 2.7.0 | [GHSA-gh4c-6fx4-qh6g](https://github.com/urllib3/urllib3/security/advisories/GHSA-gh4c-6fx4-qh6g), CVE-2026-97688 | 2.8.0 | Chunked Deflate streaming can loop without another socket read. Affects >=2.6.2,<2.8.0. |
| urllib3 2.7.0 | [GHSA-vxq7-64xx-v4gw](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw), CVE-2026-97689 | 2.8.0 | Unbounded chunk-size line buffering, including Requests streaming. Affects >=1.10.3,<2.8.0. |
| lightning 2.6.5 and pytorch-lightning 2.6.5 | [GHSA-qqmf-gpg7-g8gw](https://github.com/advisories/GHSA-qqmf-gpg7-g8gw), CVE-2026-58659; [upstream fix](https://github.com/Lightning-AI/pytorch-lightning/releases/tag/2.6.6) | 2.6.6 for both | Untrusted checkpoint instantiation; part of the optional neural dependency graph. |
| mako 1.4.1 | [GHSA-5639-2j2p-m4mx](https://github.com/sqlalchemy/mako/security/advisories/GHSA-5639-2j2p-m4mx), CVE-2026-102991 | 1.4.2 | Windows-only template URI traversal; not applicable to this app's supported macOS platform. |
| multidict 6.7.1 | [GHSA-54p9-h82j-f925](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925), CVE-2026-104874 | 6.9.1 | Reference leaks in particular C-extension items-view set operations; direct app use was not found. |

Earlier urllib3 issues
[GHSA-38jv-5279-wg99](https://github.com/urllib3/urllib3/security/advisories/GHSA-38jv-5279-wg99),
[GHSA-mf9v-mfxr-j63j](https://github.com/urllib3/urllib3/security/advisories/GHSA-mf9v-mfxr-j63j),
and [GHSA-qccp-gfcp-xxvc](https://github.com/urllib3/urllib3/security/advisories/GHSA-qccp-gfcp-xxvc)
were already fixed by the previous 2.7.0 lock. The September advisories make **2.8.0** the
smallest current security update. Requests 2.34.2 accepts `urllib3>=1.26,<3`,
and urllib3 2.8.0 requires Python >=3.10, compatible with this project's >=3.11.

## Repaired local neural contract

The installed and locked versions agree: pyannote.audio 4.0.7, huggingface-hub
1.30.0, torch 2.14.0, torchaudio 2.11.0, and torchcodec 0.16.0. Inspection of
the installed pyannote sources and an isolated offline import probe confirmed:

- `Pipeline.from_pretrained` accepts `token=`, not `use_auth_token=`.
- Community-1 is the supported local v4 pipeline. It requires its own Hugging
  Face access agreement; older 3.1 model agreements are insufficient. Setup,
  README, SETUP, and `.env.example` now use the Community-1 link. No conditions
  were accepted and no model weights were fetched during this work.
- Results expose `speaker_diarization` as an Annotation; the adapter also
  accepts a legacy bare Annotation. Existing overlap alignment is preserved.
- The pipeline accepts a waveform dictionary containing a `(1, samples)`
  float32 tensor and `sample_rate=16000`. The app now reads its ffmpeg-produced
  PCM16 mono WAV with the standard library and normalizes samples by 32768.
  This avoids the file-decoding dependency on TorchCodec and FFmpeg shared
  libraries. `ffmpeg` is still required for extracting audio from recordings.
- Pyannote still imports torchaudio and attempts to import TorchCodec, so its
  declared dependencies remain necessary. The installed imports succeeded and
  reported `TORCHCODEC_AVAILABLE=True`. In-memory input does not fix unrelated
  binary import failures; those continue to trigger the acoustic fallback.
- Initialization failure is cached, a `None` pipeline is rejected explicitly,
  and load/run failures retain acoustic fallback. New neural error logs report
  exception types without potentially exposing tokens or other exception text.
- Package initialization forces `HF_HUB_DISABLE_TELEMETRY=1`,
  `PYANNOTE_METRICS_ENABLED=0`, and `OTEL_SDK_DISABLED=true` before speech
  dependencies are imported. These are application privacy requirements rather
  than optional `.env` preferences. Tests start fresh processes with telemetry
  initially enabled and assert disabled settings at dependency import boundaries.

References: [pyannote v4 changes](https://github.com/pyannote/pyannote-audio/releases/tag/4.0.0),
[v4.0.7 output contract](https://github.com/pyannote/pyannote-audio/blob/4.0.7/src/pyannote/audio/pipelines/speaker_diarization.py),
[Community-1 model card](https://huggingface.co/pyannote/speaker-diarization-community-1),
[TorchCodec compatibility](https://github.com/meta-pytorch/torchcodec#compatibility-with-torch-versions).
The current TorchCodec table permits 0.16 with torch >=2.11; the locked torch
version satisfies that range. No torch/torchaudio/TorchCodec version changed.

## Applied dependency updates and compatibility

The approved manifest and lockfile changes were prepared from public PyPI
release metadata and then applied:

- Changed the optional pyannote declaration from `>=3.1.1` to `>=4.0.7,<5`,
  matching the verified API while retaining locked 4.0.7.
- Added uv constraint floors for urllib3 2.8.0, lightning 2.6.6,
  pytorch-lightning 2.6.6, mako 1.4.2, and multidict 6.9.1. Constraints protect
  existing transitive dependencies without adding them to the base install.
- Updated only those five locked distribution versions and their published
  artifact URLs, sizes, timestamps, and SHA-256 hashes; all other distribution
  versions and the existing dependency graph remain unchanged.

| Distribution | Before | Installed and locked |
| --- | --- | --- |
| urllib3 | 2.7.0 | 2.8.0 |
| lightning | 2.6.5 | 2.6.6 |
| pytorch-lightning | 2.6.5 | 2.6.6 |
| mako | 1.4.1 | 1.4.2 |
| multidict | 6.7.1 | 6.9.1 |

All five target releases require Python >=3.10. Their active dependency
requirements match the installed versions on Python 3.12/macOS and are
satisfied by the updated graph. The candidate passed an isolated **offline**
`uv lock --check --no-build` using the existing interpreter before approval.
After installation, `uv pip check` passed and a locked offline sync dry run
reported no changes. uv constraints apply to uv's workspace resolver;
they are not published wheel requirements for consumers using other installers.

Compatibility risks:

- [urllib3 2.8.0](https://urllib3.readthedocs.io/en/2.8.0/changelog.html) separates
  proxy TLS configuration from destination TLS configuration correctly.
  Corporate HTTPS proxy configurations relying on the previous behavior may
  need their own proxy CA/client-certificate configuration. No live proxy or
  Webex request was tested.
- Lightning now restricts checkpoint instantiators/class resolution. Both
  installed namespaces rejected a synthetic untrusted checkpoint instantiator
  in the offline probe. Actual Community-1 checkpoint loading still requires
  trusted, separately authorized model weights; offline contracts cannot
  establish checkpoint compatibility or diarization accuracy.
- multidict's updated compiled extension imported successfully and synthetic
  items-view union/subtraction checks preserved reference counts. Mako template
  rendering passed; its Windows URI validation fix was not exercised on macOS.
- The Community-1 migration may download new weights during normal first use,
  needs the new access agreement, and can change speaker-label accuracy. Until
  access and weights are available, acoustic fallback remains functional.
- Python 3.11, other interpreter versions, MPS/CUDA inference, long recordings,
  and real device/capture behavior were not exercised.

Installation used the updated lock with no builds, project reinstall, Python
downloads, or removal of unrelated environment packages:

```sh
UV_CACHE_DIR=/private/tmp/webex-obs-uv-cache uv sync --locked --extra neural-diarization --inexact --no-install-project --no-build --no-python-downloads
```

Before/after distribution inventories contain the same 122 package names; only
the five approved versions changed. The application remains editable, so source
changes are active, but its installed metadata was not rebuilt. Rebuilding would
require currently absent build tools and was excluded to keep installation
within the five approved packages. No models were downloaded, credentials read,
installed service operated, real meetings recorded, messages sent, or personal
files deleted.

## Validation results

Tests used Python 3.12.12 and uv 0.9.16. During tests, `--no-sync` prevented
implicit dependency installation and `--offline` prevented uv network access.
The focused tests mock inference and pipeline loading, use synthetic PCM in
temporary directories, and check token precedence, v4/legacy output, normalized
input, missing dependencies, cached failures, device selection, acoustic
fallback, token-safe errors, setup links, and telemetry import timing.

| Check | Exact result |
| --- | --- |
| Baseline: `UV_CACHE_DIR=/private/tmp/webex-obs-uv-cache uv run --locked --no-sync --offline pytest -q` | 215 passed in 0.68s; no failures or skips. |
| Before updates: focused / full tests | 36 passed in 0.60s / 240 passed in 0.87s; no failures or skips. |
| After updates, focused: `UV_CACHE_DIR=/private/tmp/webex-obs-uv-cache uv run --locked --no-sync --offline --no-env-file pytest -q tests/test_diarizer.py tests/test_dependency_privacy.py tests/test_transcriber.py tests/test_cli.py` | 36 passed in 0.78s; no failures or skips. |
| After updates, full: `UV_CACHE_DIR=/private/tmp/webex-obs-uv-cache uv run --locked --no-sync --offline --no-env-file pytest -q` | 240 passed in 0.79s; no failures or skips. |
| After updates: `UV_CACHE_DIR=/private/tmp/webex-obs-uv-cache uv pip check --python .venv/bin/python` | Exit 0; checked 122 packages in 28ms; all installed packages compatible. |
| After updates: installed-library offline probe | Exit 0: updated packages and real torch/torchaudio/pyannote/TorchCodec imports, `Pipeline.from_pretrained` token signature, disabled HF/pyannote telemetry, both Lightning checkpoint-instantiator guards, multidict compiled operations/reference counts, and Mako rendering passed. Network denied, fresh temporary HOME/cache, and no inherited credentials. |
| After updates: synthetic audio runtime probe | Exit 0: actual ffmpeg extraction from generated 48 kHz stereo PCM to 16 kHz mono; actual NeuralDiarizer normalization/tensor conversion and real DiarizeOutput adaptation with a stub pipeline passed. No model loading, recording, or model inference. |
| Applied patch verification: `git apply --reverse --check proposed-dependencies.patch` | Exit 0; approved manifest/lock patch applied exactly. The obsolete proposal was moved outside the working tree after this check. |
| Isolated candidate: `uv lock --check --offline --no-build --python <existing .venv/bin/python>` | Exit 0; resolved 148 packages in 23ms; candidate lock unchanged. Two sandbox attempts first hit uv's macOS system-configuration panic (101); the same offline check succeeded outside the sandbox. |
| After updates: `UV_CACHE_DIR=/private/tmp/webex-obs-uv-cache uv sync --locked --extra neural-diarization --inexact --no-install-project --no-build --no-python-downloads --dry-run --offline` | Exit 0; resolved 148 packages in 14ms, audited 121 in 21ms; would make no changes. uv's sandboxed sync preview first hit the same macOS panic; preview, installation, and final dry run succeeded outside the sandbox. |
| After updates: public OSV scan | 147 locked registry entries queried; zero non-withdrawn advisory matches. |
| Final diff checks | `git diff --check` passed. Final status contains only the focused code/tests/docs and approved manifest/lock changes. No secrets or generated media were added. |

There were no existing suite failures. End-to-end model loading/inference,
accelerator execution, Windows-specific Mako behavior, and live integration
checks remain unavailable under the task's restrictions. No application run or
service restart was used as validation.
