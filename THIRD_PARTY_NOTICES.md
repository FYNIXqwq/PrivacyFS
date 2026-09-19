# Third-party notices

PrivacyFS uses third-party libraries and includes third-party build resources.
PrivacyFS's own code is licensed under the [MIT License](LICENSE), copyright
2026 FYNIXqwq. Third-party components remain subject to their respective
licenses; the project license does not replace their notices or conditions.

## Wails

- Project: [Wails](https://github.com/wailsapp/wails)
- Version inspected: `github.com/wailsapp/wails/v3 v3.0.0-beta.14`
- License: MIT
- Copyright: 2018–Present Lea Anthony
- Full license: [LICENSES/Wails-MIT.txt](LICENSES/Wails-MIT.txt)

`wails-browser/build/` contains Wails build resources and adapted/generated
templates. In particular, `appicon.png` and
`appicon.icon/Assets/wails_icon_vector.svg` match the inspected upstream resources
byte for byte. The build tasks and Windows installer templates have project
customizations; they are not represented as unmodified upstream files.

The NSIS helper retains its attribution to the
[file-association macros by nikku](https://gist.github.com/nikku/281d0ef126dbc215dd58bfd5b3a5cd5b).
The upstream gist did not expose a separate license in this audit; its provenance
remains a follow-up item and is not independently certified by this notice.

## Other dependencies and optional integrations

Declared Python dependencies are listed in `pyproject.toml`; Go dependencies are
listed in `wails-browser/go.mod`. `evals/pi/pi-example.mjs` optionally imports
`@earendil-works/pi-agent-core` (the inspected 0.84.4 reference is MIT licensed).
Those declarations do not mean their complete source or binary distributions
are included in this repository.

Important license differences in the inspected dependencies:

- `pathspec` 1.1.1 uses MPL-2.0. Its file-level conditions remain applicable;
  see the [Mozilla MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).
- `regex` 2026.9.3 declares `Apache-2.0 AND CNRI-Python`; both notices matter.
- The optional Slint 1.9.2a1 package declares GPL-3.0-only, Slint Royalty-free
  2.0, or Slint Software 3.0 as alternatives. Slint's local UI is excluded from
  version control. Distributing it still requires an applicable license choice;
  the [royalty-free terms](https://slint.dev/agreements/slint-royalty-free-license.pdf)
  include attribution conditions.
- HanLP's code is Apache-2.0, but its models normally have separate terms;
  consult [HanLP's license statement](https://github.com/hankcs/HanLP#license).
  A model loader's license does not grant rights to the model weights.

This file is not a complete binary-distribution license bundle. In particular,
the inspected local jieba wheel lacked its MIT license file, and the local
llama-cpp-python wheel included its binding license but not a separate
llama.cpp/ggml notice. These materials must be checked and supplied when
redistributing the corresponding components. Model weights, local environments
and compiled executables are excluded from version control. Preserve the
applicable third-party copyright and license notices in distributions.
