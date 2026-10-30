# Fonts for the quote PDF

`DejaVuSans.ttf` and `DejaVuSans-Bold.ttf` are subsets of DejaVu Sans 2.37 (Bitstream Vera licence plus
public-domain DejaVu changes; see `LICENSE_DEJAVU`, which must stay next to the fonts). They exist because the
PDF's built-in Helvetica has no naira sign (U+20A6) and prints a black square for it.

Source: the `matplotlib` wheel on PyPI (`mpl-data/fonts/ttf/`), original sha256
`3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22` (regular),
`b184b89e3c1075f22f6b71575b6fc20d4972b3cfd3b23322ca6fd596dcaef167` (bold).

Subset (fontTools `pyftsubset`, no hinting) to: Basic Latin, Latin-1, Latin Extended-A/B (U+0020-024F),
combining marks (U+0300-036F), Latin Extended Additional (U+1E00-1EFF, Yoruba diacritics),
General Punctuation (U+2000-206F), Currency Symbols (U+20A0-20CF) and the minus sign (U+2212).
The file names keep "DejaVu"; the licence only forbids a modified font carrying the "Bitstream" or "Vera" names.
