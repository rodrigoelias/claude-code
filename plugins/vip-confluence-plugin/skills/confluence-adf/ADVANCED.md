# confluence-adf — Advanced: bulk render/apply workflow

Use `render` + `apply` when editing multiple sections at once. For single-section edits, use `edit` directly.

## `render` — dump full page as annotated markdown

Produces a single markdown file with `<!-- @path: ... -->` markers at every block node boundary.

```bash
confluence-adf render <page_id_or_url>
confluence-adf render <page_id_or_url> --output page.md
confluence-adf render <page_id_or_url> --draft
```

Output includes YAML front-matter (page_id, version, title, url, rendered_at) followed by marked regions. Editable regions (paragraph, heading, codeBlock, bulletList, orderedList) can be modified locally. Readonly regions (table, blockquote, mediaSingle, etc.) are marked `readonly` and skipped during apply.

## `apply` — reconcile annotated markdown edits back to the page

After editing a rendered file locally, push changes back:

```bash
confluence-adf apply <page_id_or_url> edited.md --dry-run
confluence-adf apply <page_id_or_url> edited.md
confluence-adf apply <page_id_or_url> edited.md --baseline original.md
```

Flags:
- `--dry-run` — show detected edits without applying.
- `--baseline <file>` — use a local original instead of re-fetching (faster when you still have the original render output).

Detects version conflicts (file version vs current page version) and rejects readonly region edits with warnings. Edits are applied in reverse document order to preserve path ID validity.
