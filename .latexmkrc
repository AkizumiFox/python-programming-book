# latexmk configuration for book build
# Requires: xelatex, biber, Python pygments (for minted)
#
# `make` always compiles with cwd = build/, so aux/log/minted files never
# land in src/.  This file is only consulted when latexmk is invoked from
# the project root (cwd contains src/book.tex).  It must not override a
# TEXINPUTS the Makefile already set.

$pdf_mode = 5;

$xelatex = 'xelatex -shell-escape -interaction=nonstopmode -synctex=1 %O %S';

$biber = 'biber --output_safechars %O %S';

$max_repeat = 5;

$biber_fudge = 1;

@generated_exts = (@generated_exts, 'synctex.gz', 'listing', 'pyg');

$silence_logfile_warnings = 0;

if (-f 'src/book.tex' && -d 'src/packages') {
    $out_dir = 'build';
    $aux_dir = 'build';
    $ENV{TEXINPUTS} = 'src:src/packages:' unless $ENV{TEXINPUTS};
}
