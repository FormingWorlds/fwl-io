# Mirror a deposit to Dataverse

Zenodo is the primary source of every dataset. DataverseNL is a download mirror: the second link in the fetch fallback chain, so a dataset stays reachable when Zenodo is unavailable. This guide covers mirroring one pinned Zenodo deposit into the Proteus Framework collection.

## When to mirror

Mirror a dataset once its Zenodo version DOI is pinned in a manifest and you want a second download source for it. A dataset that lists only a `zenodo` DOI works, but is single-sourced; adding a `dataverse` DOI makes the fetch chain fall back to the mirror.

## Running the mirror

Mirroring runs from the **Mirror a Zenodo deposit to Dataverse** GitHub Actions workflow, not from a laptop. The Dataverse API token is a protected environment secret that lives only in CI, so no one needs personal upload rights to the collection; who may run the workflow is the access control.

1. Open the workflow in the Actions tab and run it, supplying the Zenodo version DOI and the target collection alias.
2. Leave **publish** checked to publish the dataset (its files become downloadable), or uncheck it to create a private draft you inspect first. The first time you mirror a new kind of deposit, run with **dry run** checked to confirm the download and metadata mapping without touching Dataverse.
3. The run prints the Dataverse DOI. Add it to the dataset's manifest entry:

    ```toml
    [star.tracks.baraffe_2015]
    subdir = "star/tracks/baraffe_2015"
    zenodo = "10.5281/zenodo.15729114"
    dataverse = "10.34894/XXXXXX"        # the printed mirror DOI
    ```

    Commit that change in a pull request, like any other data change.

## What the mirror does

For the given Zenodo version DOI, the mirror downloads and checksum-verifies every file, creates a Dataverse dataset whose title, authors, and description come from the Zenodo record (with a note recording the source DOI), uploads the files byte-identically with tabular ingest disabled, and publishes the dataset unless asked not to. A concept DOI is rejected, so the mirror always tracks a specific pinned deposit.

## Local dry run

To check the Zenodo side and the metadata mapping without any Dataverse access, run a dry run locally:

```bash
fwl-io mirror 10.5281/zenodo.15729114 --collection Proteus_Fr --dry-run
```

This downloads the files and builds the citation metadata, then stops before any Dataverse write, so it needs no token.

A real local run (with or without `--no-publish`) does create a Dataverse dataset, so it needs both a token and a contact email: pass `--contact-email` and set `DATAVERSE_TOKEN`. Only the dry run above is exempt. The GitHub Actions workflow supplies both from the `dataverse` environment secrets, so its runs already satisfy this.
