# Mirror a deposit to Dataverse

Zenodo is the primary source of every dataset. DataverseNL is a download mirror: the second link in the fetch fallback chain, so a dataset stays reachable when Zenodo is unavailable. This guide covers mirroring one pinned Zenodo deposit into the Proteus Framework collection.

## When to mirror

Mirror a dataset once its Zenodo version DOI is pinned in a manifest and you want a second download source for it. A dataset that lists only a `zenodo` DOI works, but is single-sourced; adding a `dataverse` DOI makes the fetch chain fall back to the mirror.

## Running the mirror

Mirroring runs from the **Mirror a Zenodo deposit to Dataverse** GitHub Actions workflow, not from a laptop. The Dataverse API token is a protected environment secret that lives only in CI, so no one needs personal upload rights to the collection; who may run the workflow is the access control.

1. Open the workflow in the Actions tab and run it, supplying the Zenodo version DOI and the target collection alias. To mirror only some files of the deposit, list their names in **files**, separated by spaces (see [Mirroring part of a deposit](#mirroring-part-of-a-deposit)).
2. Leave **publish** checked to publish the dataset (its files become downloadable), or uncheck it to create a private draft you inspect first. The first time you mirror a new kind of deposit, run with **dry run** checked to confirm the download and metadata mapping without touching Dataverse.
3. The run prints the Dataverse DOI. Add it to the dataset's manifest entry, then run `fwl-io check-mirrors` to confirm the pin serves the dataset:

    ```toml
    [star.tracks.baraffe_2015]
    zenodo = "10.5281/zenodo.15729114"
    dataverse = "10.34894/XXXXXX"        # the printed mirror DOI
    ```

    Commit that change in a pull request, like any other data change.

## When a run fails

DataverseNL sometimes answers an API call with its bot-check page (an HTML page titled "Oh noes!"), a 429 (too many requests), a 502, 503 or 504 gateway error, or not at all, and a proxy on the way can drop the connection. The mirror makes up to 5 attempts in total at a file upload, a file deletion, the draft deletion and the reads it makes to check them, waiting 30, 60, 120 and 240 s in between (after a 429, the wait the server asks for in whole seconds, at most 300 s). Before it sends a file again, it lists the draft and skips a file that arrived. In a run without **into**, a different file of the same name stops the run; so does a file whose checksum type the mirror does not know (it knows MD5, SHA-1, SHA-256 and SHA-512), since it counts as a different file. Each file is checked in the draft right after its upload; after the uploads, with or without publishing, the run checks that the draft holds no file the deposit does not.

The publish request is sent again only after the bot-check page at a status below 500 or a 429, which show that DataverseNL did not process it. After any other reply except a 4xx rejection, a success included, or no reply, the request is not sent again: the mirror checks the dataset state up to 5 times over 450 s, and the publish counts as done only when the state is RELEASED. If it is not, or if all 5 attempts get the bot-check page or a 429, the publish is not confirmed. A 4xx reply other than the bot-check page is a rejection.

The run logs the DOI of the dataset it creates. The dataset creation is not repeated, since a repeat could create a second draft: when it fails, look in the collection for a draft the run did not report. When a later step fails while the draft it created holds none of its files, the run deletes the draft, with the same retries, and the error names the call and the last response. Once a file has reached the draft, or may have (an upload that failed but whose file the draft lists, or a draft that cannot be listed), a failure keeps it: the error names the files still missing (see below). The draft counts as deleted only when DataverseNL itself answers 404; if the deletion fails, the log names the draft to delete by hand. The run keeps the dataset, logs its DOI and asks you to check its state by hand when the publish is not confirmed, when the dataset was already published before the publish request, or when the run is interrupted.

## Publishing a reviewed draft

A draft created with **publish** unchecked stays private until it is published. Run the **Publish an existing Dataverse draft** GitHub Actions workflow, supplying the draft's persistent id (the DOI printed by the mirror run, with a `doi:` prefix, for example `doi:10.34894/XXXXXX`). It only publishes; it never creates a dataset, so it cannot mint a duplicate one. Add the DOI to the manifest as in step 3 above once it is published. If the dataset is not RELEASED after its publish request, the run stops with an error saying the publish was not confirmed: check the dataset's state on DataverseNL before running it again.

## What the mirror does

For the given Zenodo version DOI, the mirror downloads and checksum-verifies each file before it uploads it, creates (once the first file has arrived, so a Zenodo outage writes nothing) a Dataverse dataset whose title, authors, and description come from the Zenodo record (with a note recording the source DOI), uploads the files byte-identically with tabular ingest disabled, and publishes the dataset unless asked not to. Dataverse unpacks an uploaded zip archive into its members, so the mirror sends each file whose name ends in `.zip` inside a second, uncompressed zip, built in temp space that needs room for one copy of the archive; Dataverse unpacks that one and keeps the archive as one file with its own name and checksum. A zip archive under another name is sent as it is; if Dataverse unpacks it, the draft check stops the mirror. A concept DOI is rejected, so the mirror always tracks a specific pinned deposit. The dataset gets the license of the Zenodo record: the mirror reads it from the record, picks the license the Dataverse server lists with the same URL or SPDX identifier, and sets it right after creating the draft. A record whose license the server does not list, or that lists no license or several, stops the mirror before any draft is created. Where the record's author licenses it otherwise (for example a Zenodo licence field the server does not list), fill **licence** in the workflow (`--licence NAME` on the command line) with the Dataverse license name: the draft gets that license and a description line naming it and the Zenodo licence field. It needs a record with exactly one Zenodo license entry (a record with none or several still stops the mirror), and is refused for a record whose Zenodo license matches one or more licenses the server lists, and with **into**, **dry run** or a publish: the workflow leaves such a dataset a draft whatever **publish** says (pass `--no-publish` on the command line), so the draft is reviewed first.

## Large deposits and finishing a partial draft

The run holds one file at a time on disk: it downloads a file, uploads it, checks it in the draft and deletes the local copy. Uploads into one draft are 60 s apart, 600 s after an upload that met the bot-check page (on any request from the start of that upload to its check: the deletion of a differing copy, the upload, the listings), and after two such uploads in a row the run stops before the next upload, with the draft kept. A file whose check after its upload fails is deleted from the draft. Once a file of the run has reached the draft, no failure deletes the draft, the publish included; the error names the files missing, wrong (not of the Zenodo size or checksum) and not selected. To finish a kept draft, run the mirror again with the draft's DOI and the same files and server: fill **into** in the workflow, or from the command line:

```bash
fwl-io mirror 10.5281/zenodo.17674612 --collection Proteus_Fr --into doi:10.34894/XXXXXX
```

It accepts only a draft that was never published and whose description names the same Zenodo record. A file the draft holds with the Zenodo name and size (and the registry checksum, where the draft lists a checksum of the same type) is kept without a download, any other is downloaded, compared and, if it differs, sent again, and a file the Zenodo record does not hold is deleted, so a second run into a complete draft sends nothing. A file kept where the draft lists no checksum of the registry type is compared by size only. DataverseNL lists SHA-1 and the registries hold MD5, so on DataverseNL every kept file is compared by size only. Check the draft's contents before you publish it: download each file from DataverseNL and compare its MD5 with the registry. A draft whose publish fails is kept; publish it with the workflow below. Each deletion is logged. The draft is never deleted and never published: publish it with the **Publish an existing Dataverse draft** workflow below. **into** takes no dry run. Workflow runs for the same DOI, or into the same draft, wait for each other, and a second waiting run replaces the first waiting one.

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
