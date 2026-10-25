# historical — CSV exports from the phone app

Put the CSV files the Govee phone app exported here, one per thermometer.
`thermview` lists them on the devices screen (`/devices`), and choosing one for
a device and pressing Save stores it — the same importer `thermwatch.py --import`
uses, so the header is matched loosely and rows already in the store are not
written twice.

Equivalent from the command line:

```
python thermwatch\thermwatch.py --import thermwatch\historical\<file>.csv --device <name>
```

The files are data, not part of the tool. If a repository should not carry them,
add `historical/*.csv` to its `.gitignore`; this README is what keeps the folder
itself in the repo, so the feature has somewhere to put files.
