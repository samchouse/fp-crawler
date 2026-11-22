# FP-Scraper

Scrapes FP TOP 10 catalogs, resolves titles to TMDB IDs, and writes one JSON file per region.

## Usage

Install the project:

```bash
python -m pip install .
```

Scrape the global page:

```bash
python -m flixpatrol_scraper --region global --output global.json
```

Scrape one region:

```bash
python -m flixpatrol_scraper --region united-states --output united-states.json
```

Scrape multiple regions:

```bash
python -m flixpatrol_scraper --region global,united-states,brazil --output exports
```

Scrape all supported regions:

```bash
python -m flixpatrol_scraper --all-regions --output exports
```

Scrape all supported regions except regions known to have no or low catalog coverage:

```bash
python -m flixpatrol_scraper --all-regions-slim --output exports
```

Resolve TMDB IDs while scraping:

```bash
python -m flixpatrol_scraper --all-regions --output exports --resolve-tmdb --tmdb-api-key YOUR_TMDB_API_KEY
```

Ambiguous TMDB matches are checked against TMDB's streaming-provider availability for the chart's platform and country. TMDB's watch-provider data is powered by JustWatch; attribute JustWatch when presenting that provider data.

Title matching accepts shortened or expanded names when the meaningful words align (for example, `Matchbox` and `Matchbox: The Movie`). The resolver uses the chart's platform and country, release date, TMDB popularity, and vote count to rank candidates. Popularity alone does not decide a match; ambiguous candidates stay unresolved unless the combined evidence has a clear lead. Title resolution uses TMDB only and does not fetch FlixPatrol title pages.

## GitHub Actions

The repo includes a scheduled workflow in [.github/workflows/update-catalogs.yml](.github/workflows/update-catalogs.yml).

It:

- runs once per day at `15:30 UTC`
- can also be triggered manually with `workflow_dispatch`
- scrapes `--all-regions-slim`
- resolves TMDB IDs
- writes outputs into `catalogs/`
- commits and pushes updated catalog files back to the repository

Required GitHub secrets:

- `TMDB_API_KEY`, or
- `TMDB_ACCESS_TOKEN`
