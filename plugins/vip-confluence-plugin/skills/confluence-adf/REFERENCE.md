# confluence-adf — Reference

## Supported top-level node types for `edit`

- `paragraph` (with `**strong**`, `*em*`, `` `code` ``, `[link](url)` inline marks)
- `heading` (`#` through `######`)
- `codeBlock` (fenced ```` ``` ```` with optional language)
- `bulletList` / `orderedList` of single-paragraph list items
- `expand` via fenced container: `::: expand title="..."` ... `:::`
- `panel` via fenced container: `::: panel type=info` ... `:::`
- `panel` with custom background color: `::: panel type=custom color=#hex` ... `:::`
- `panel` with named color: `::: panel type=custom color=dark-red` ... `:::`

Anything outside this set (tables, blockquotes, task lists, raw HTML, nested expands, horizontal rules) will fail loudly with a markdown error.

## Panel color named presets

| Light | Medium | Dark |
|-------|--------|------|
| `light-blue` #DEEBFF | `blue` #B3D4FF | `dark-blue` #4C9AFF |
| `light-teal` #E6FCFF | `teal` #B3F5FF | `dark-teal` #79E2F2 |
| `light-green` #E3FCEF | `green` #ABF5D1 | `dark-green` #57D9A3 |
| `light-yellow` #FFFAE6 | `yellow` #FFF0B3 | `dark-yellow` #FFC400 |
| `light-red` #FFEBE6 | `red` #FFBDAD | `dark-red` #FF8F73 |
| `light-purple` #EAE6FF | `purple` #C0B6F2 | `dark-purple` #998DD9 |
| `light-gray` #F4F5F7 | `gray` #B3BAC5 | `white` #FFFFFF |

Panel type names also work as color shortcuts: `info`, `note`, `tip`, `success`, `warning`, `error`.

## Inline status lozenge valid colors

`neutral` (gray) · `blue` · `green` · `yellow` · `red` · `purple`

Do not use hex codes or panel color names — the CLI will reject them. When the user asks for "orange", use `yellow` or `red` as the closest match and explain the limitation.
