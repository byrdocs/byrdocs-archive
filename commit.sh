#!/bin/bash
set -e
cd `dirname $0`
cd metadata
tmpfile=$(mktemp)
pdm run metadata.py -f ../metadata.ods -o ../metadata.json -d -c $tmpfile
cd ..
git add metadata.json metadata.ods
if [ -s "$tmpfile" ]; then
    git commit -e --template=$tmpfile $@
fi
rm $tmpfile
