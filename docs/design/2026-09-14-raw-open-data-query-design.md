# Raw Open Data Query Design

## Goal

Make every downloaded Taipower resource discoverable and readable without weakening the
existing trusted Text2SQL database. The current six curated sources remain the default
query surface; the raw layer is an explicit, read-only query scope.

## Architecture

`RawDataService` reads `data/raw/manifest.json`, resolves each manifest path under the
snapshot directory, and writes a small SQLite catalog at
`data/processed/raw_open_data.db`. The catalog stores provenance, format, size, parse
status, discovered columns, and a bounded preview. It does not duplicate the 928 MiB
snapshot or force unrelated schemas into one table.

Rows are read lazily from the original CSV, JSON, XML, or ZIP file. Every response carries
the resource id and source metadata. ZIP resources expose a member listing first and allow
a selected structured member to be read. Paths are resolved and checked against approved
snapshot roots before opening.

Files placed in `data/raw/inbox` are cataloged as independent local resources on rebuild.
They are not merged into trusted facts automatically. Promotion into `power.db` continues
to require an explicit slot, field mapping, validation, and review.

## Public and Administrative Boundaries

Anonymous users may list resources, inspect catalog status, read bounded rows, and submit
a query with `query_scope: "raw"`. Rebuilding the catalog is an authenticated,
CSRF-protected administrative mutation. Raw reads never enable online models or data
management actions.

## Query Behavior

The existing `trusted` scope remains the default. Raw natural-language queries rank
catalog entries using normalized title and character-overlap matching, then return a
traceable resource list. Supplying an exact resource id reads that resource. Responses
state that raw data is uncurated and may retain source-specific units, names, and dates.

## Errors and Limits

Unsupported, missing, malformed, oversized, or unsafe resources return structured errors
without exposing local absolute paths. Row reads are bounded to 200 rows. CSV and XML are
streamed; JSON is bounded before parsing; ZIP members are path-checked and size-bounded.

## Testing

Tests create a temporary four-format snapshot and prove catalog coverage, schema isolation,
lazy row reads, public read access, raw query routing, and admin protection on rebuild.
