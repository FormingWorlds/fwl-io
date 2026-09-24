# Mirror a deposit to Dataverse

Zenodo is the primary source of every dataset. DataverseNL is a download mirror: the second link in the fetch fallback chain, so a dataset stays reachable when Zenodo is unavailable. This guide covers mirroring one pinned Zenodo deposit into the Proteus Framework collection.

## When to mirror

Mirror a dataset once its Zenodo version DOI is pinned in a manifest and you want a second download source for it. A dataset that lists only a `zenodo` DOI works, but is single-sourced; adding a `dataverse` DOI makes the fetch chain fall back to the mirror.

## Running the mirror

Mirroring runs from the **Mirror a Zenodo deposit to Dataverse** GitHub Actions workflow, not from a laptop. The Dataverse API token is a protected environment secret that lives only in CI, so no one needs personal upload rights to the collection; who may run the workflow is the access control.

1. Open the workflow in the Actions tab and run it, supplying the Zenodo version DOI and the target collection alias. To mirror only some files of the deposit, list their names in **files**, separated by spaces (see [Mirroring part of a deposit](#mirroring-part-of-a-deposit)).
2. Leave **publish** checked to publish the dataset (its files become downloadable), or uncheck it to create a private draft you inspect first. The first time you mirror a new kind of deposit, run with **dry run** checked to confirm the download and metadata mapping without touching Dataverse.
3. The run prints the Dataverse DOI. Add it to the dataset's manifest entry:

    ```toml
    [star.tracks.baraffe_2015]
    zenodo = "10.5281/zenodo.15729114"
    dataverse = "10.34894/XXXXXX"        # the printed mirror DOI
    ```

    Commit that change in a pull request, like any other data change.

## When a run fails

DataverseNL sometimes answers an API call with its bot-check page (an HTML page titled "Oh noes!"), a 502, 503 or 504 gateway error, or not at all. The mirror repeats a file upload, the publish and the draft deletion up to 5 times, waiting 30, 60, 120 and 240 s in between. Before it sends a file again, it lists the draft and skips a file that arrived; a different file of the same name stops the run. Before it publishes, it checks that the draft holds exactly the Zenodo files, with the same sizes and checksums.

The dataset creation is not repeated, since a repeat could create a second draft: when it fails, look in the collection for a draft the run did not report. When a later step fails, the run deletes the draft it created, with the same retries, and the error names the call and the last response. If that deletion also fails, the log names the draft to delete by hand. If no publish reply gets through, the dataset may be public already, so the run keeps it and logs its DOI. Check its state by hand.

## Publishing a reviewed draft

A draft created with **publish** unchecked stays private until it is published. Run the **Publish an existing Dataverse draft** GitHub Actions workflow, supplying the draft's persistent id (the DOI printed by the mirror run, with a `doi:` prefix, for example `doi:10.34894/XXXXXX`). It only publishes; it never creates a dataset, so it cannot mint a duplicate one. Add the DOI to the manifest as in step 3 above once it is published.

## What the mirror does

For the given Zenodo version DOI, the mirror downloads and checksum-verifies every file, creates a Dataverse dataset whose title, authors, and description come from the Zenodo record (with a note recording the source DOI), uploads the files byte-identically with tabular ingest disabled, and publishes the dataset unless asked not to. A concept DOI is rejected, so the mirror always tracks a specific pinned deposit.

## Mirroring part of a deposit

When the manifest entry sets `files`, mirror the same files so the mirror matches what the dataset fetches. In the workflow, fill **files**; file names in it must not contain spaces. From the command line, pass each name with `--file`:

```bash
fwl-io mirror 10.5281/zenodo.22776069 --collection Proteus_Fr --file name1 --file name2
```

## Local dry run

To check the Zenodo side and the metadata mapping without any Dataverse access, run a dry run locally:

```bash
fwl-io mirror 10.5281/zenodo.15729114 --collection Proteus_Fr --dry-run
```

This downloads the files and builds the citation metadata, then stops before any Dataverse write, so it needs no token.

A real local run (with or without `--no-publish`) does create a Dataverse dataset, so it needs both a token and a contact email: pass `--contact-email` and set `DATAVERSE_TOKEN`. Only the dry run above is exempt. The GitHub Actions workflow supplies both from the `dataverse` environment secrets, so its runs already satisfy this.
