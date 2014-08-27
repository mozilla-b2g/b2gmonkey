# b2gmonkey

b2gmonkey is a tool for automated monkey testing of
[Firefox OS](https://developer.mozilla.org/en-US/docs/Mozilla/Firefox_OS).

## Prerequisites

You will need a
[Marionette enabled Firefox build](https://developer.mozilla.org/en-US/docs/Marionette/Builds)
with 
[Orangutan](https://github.com/mozilla-b2g/orangutan) installed.

## Installation

Installation is simple:

    pip install -e git://github.com/davehunt/b2gmonkey.git#egg=b2gmonkey

If you anticipate modifying b2gmonkey, you can instead:

    git clone git://github.com/davehunt/b2gmonkey.git
    cd b2gmonkey
    python setup.py develop

## Running

    Usage: b2gmonkey [options]

    For full usage details run `b2gmonkey --help`.
