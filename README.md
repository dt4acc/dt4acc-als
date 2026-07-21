# Digital twin for ALS based on dt4acc

## Requirements

* dt4acc (from https://github.com/dt4acc/dt4acc) branch `next/main`
* dt4acc-lib (from https://github.com/dt4acc/dt4acc-lib) branch `next/main`
* bact_mml_json_importer (from https://github.com/hz-b/bact-mml-importer)
  branch `dev/feature/family-model-convienience-attributes`: stay tuned as the part needed from this package will be made 
  available as separate package

## Installing the twin

* create a python virtual environment
* install `dt4acc-als using pip or an pypa compatible tool

## Starting the twin

run:

```python

python3 -m dt4acc_als.twin.run_als_twin
```

