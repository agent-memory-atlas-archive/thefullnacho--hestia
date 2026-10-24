# Library: grounded answers from extension publications

Status: designed 2026-10-23, not built. The first slice is deliberately small.

## The idea

Hestia answers garden, soil and outdoor-skills questions from model memory today. The model is
often right, sometimes confidently wrong, and it never says where an answer came from. University
extension services publish exactly the material a homestead needs: tested, regional, free to read.
A local library of those publications, searched by code and cited in every answer, turns "the
model thinks" into "the extension says, page 4".

Joined with the inventory (tools, seeds, products, what is planted, all already rows in
`records_store`), the brain can answer the questions that matter most: not "how do you treat
powdery mildew" but "what can I do about the mildew on the squash with what I already have".

This keeps the project's split. Code finds the passages and the inventory rows. The model
reasons over what code handed it, and cites it.

## Shape

- **Corpus:** PDFs chosen by hand, stored under `data/library/` (gitignored). Publications are
  free to read but not licensed for redistribution, so nothing from them enters the repo. A
  tracked `library.example.toml` shows the manifest format; the real manifest is gitignored.
- **Manifest, one entry per document:** title, institution, publication number, year, region
  (state, and hardiness zone where it applies), topic, source URL. Region and year are not
  decoration: they rank results and travel with every citation.
- **Ingest (offline, by hand):** extract text per page, chunk by heading or about 300 words with
  overlap, keep doc and page on every chunk. Store in `hestia.db` beside the other stores.
- **Search is hybrid:** SQLite FTS5 (BM25) plus EmbeddingGemma vectors, merged by reciprocal rank.
  Extension text is heavy with exact terms (species names, products, "Brix"), where keyword search
  wins; vectors carry the "my squash leaves have white powder" phrasing. EmbeddingGemma runs on
  the CPU, so the 5080 stays the brain's, and it is the same embedder as the small router.
  Results from the household's own region rank first.
- **Tool:** `library` (read-only) returns the top passages with title, institution, year and
  page. A scoping skill routes garden and outdoor questions to it, as `garden_bed` does today.
- **Citations are checked in code:** an answer built on library passages must name a source it
  was given. A cited source that was not in the passages is caught the same way an unbacked
  "logged it" is caught today.
- **Inventory join:** for "with what I have" questions, code pulls matching inventory rows
  (products, tools, seeds) alongside the passages. No vectors needed for that half.

## Rules the answers keep

- **Regulations carry a year.** Hunting, trapping and fishing rules are state law and change
  every season. Passages about them are cited with the publication year and the answer says to
  check the current regulations.
- **No "safe to eat" from retrieval.** For wild edibles and mushrooms, Hestia cites and describes.
  It never clears something as edible on the strength of a passage.
- **Medication is cited, never advised.** Emergency-preparedness publications cover keeping a
  supply of life-sustaining medication through an outage. Hestia cites what they say about
  planning and storage, and sends dose, substitution or stopping questions to a pharmacist or
  doctor. Code backs this, not just the prompt.
- **Out-of-region advice is labelled** as such, not presented as local.

## Network

Nothing in the hot path. The corpus is downloaded once, by the operator, from publications they
picked; the brain never fetches a document and the model never adds one. See SECURITY.md,
"The library".

## First slice

1. 20 to 30 PDFs from the household's own state extension: what is in the ground now, soil
   biology and testing, the pests already on record.
2. 30 real questions, taken from chat history and the utterance log, each with the passage a
   good answer needs. Written before any search code, never tuned against.
3. Ingest, the `library` tool, keyword-only search first.
4. Score keyword-only against hybrid on the 30, at K>=5. Hybrid earns its dependency only if it
   clearly wins.
5. Then the inventory join, then the wider corpus (survival skills, camping, hunting, trapping).

Steps 1 and 2 are the operator's: picking sources and writing questions needs someone who knows
the garden.
