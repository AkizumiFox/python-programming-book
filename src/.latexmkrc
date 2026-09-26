# Any latexmk run from src/ (editor auto-build, etc.) must not drop
# aux/log/pdf next to the sources.  `make` itself compiles from ../build.
$pdf_mode = 5;
$out_dir = '../build';
$aux_dir = '../build';
$xelatex = 'xelatex -shell-escape -interaction=nonstopmode -synctex=1 %O %S';
$biber = 'biber --output_safechars %O %S';
$ENV{TEXINPUTS} = '.:packages:' unless $ENV{TEXINPUTS};
