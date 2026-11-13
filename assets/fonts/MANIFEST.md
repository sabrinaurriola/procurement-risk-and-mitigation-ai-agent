# Fonts for the PDF export (binaries are NOT committed)

`src/bridge/pdf.py` embeds **DejaVu Sans** (regular, bold, oblique, bold-oblique) and **DejaVu Sans Mono** so the PDF can show Latin, Greek, Cyrillic, arrows and math symbols.

| Item | Value |
|---|---|
| Source (official project release) | https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip |
| Release | DejaVu fonts 2.37 (30 Jul 2016) |
| Archive SHA-256 | `7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a` |
| License | Bitstream Vera license + public-domain DejaVu changes (full text: `DEJAVU-LICENSE.txt`, tracked). Free to use, embed in documents and redistribute with the license text; modified fonts must be renamed. Nothing is modified here. |

| File | SHA-256 |
|---|---|
| `DejaVuSans.ttf` | `7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954` |
| `DejaVuSans-Bold.ttf` | `e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724` |
| `DejaVuSans-Oblique.ttf` | `4af75fa16ee6d3ad43e1ecec41862c24954af26a55c6bb1ebb27bd486a50f5f4` |
| `DejaVuSans-BoldOblique.ttf` | `eb436dca0c2594b73d8b603b892e374fdfd8d885d25ffb4f18df4c4c0b49e50f` |
| `DejaVuSansMono.ttf` | `b4a6c3e4faab8773f4ff761d56451646409f29abedd68f05d38c2df667d3c582` |

The `.ttf` files are git-ignored. To (re)create them: `python -m src.bridge.fonts --fetch` downloads the archive above, checks its SHA-256 and every file hash, and writes the files here. The exporter refuses fonts whose hash differs. `PDF_FONT_DIR` (env name only) can point to another directory holding the same files.
