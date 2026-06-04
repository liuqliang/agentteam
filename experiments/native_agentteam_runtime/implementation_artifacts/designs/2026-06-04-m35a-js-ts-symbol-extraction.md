# M35a JavaScript and TypeScript Symbol Extraction Implementation Notes

## Goal

Extend repository symbol summaries beyond Python for JavaScript and TypeScript
projects without introducing heavyweight runtime dependencies.

## Implemented Behavior

The repo map now includes tracked `.js`, `.jsx`, `.ts`, and `.tsx` files in
`symbols.json` when they fit within the existing `max_file_bytes` limit.

For those files, the extractor records:

- ES module import targets;
- top-level or exported function declarations;
- class declarations;
- simple class method declarations.

The summary shape remains `repo_symbols.v1`, so repo context ranking can use the
same symbol/objective matching path across Python and JS/TS.

## Conservative Fallback

The extractor is a line-oriented regex scanner. It intentionally does not claim
full JavaScript or TypeScript parsing. Unsupported syntax simply produces fewer
symbols while preserving the inventory entry. Unsupported languages remain
inventory-only.

The symbol extraction version changed so clean-cache reuse cannot mix old
Python-only symbol maps with the new multi-language summaries.

## Validation

A regression test builds a small TypeScript repository and verifies that
`symbols.json` reports imports, a class, a method, and an exported function
without embedding source body content.
