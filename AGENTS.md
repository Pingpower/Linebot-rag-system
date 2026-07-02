# Agent Instructions — 工作秘書模式

## Role
Full-stack work secretary: writing, design, system tools.
Workspace: `/home/pipadmin/文件/`

## Package Manager
Use **npm**: `npm install`, `npm run dev`

## Commit Attribution
AI commits MUST include:
```
Co-Authored-By: Antigravity <noreply@antigravity.dev>
```

## File-Scoped Commands
| Task | Command |
|------|---------|
| Format MD | `npx prettier --write path/to/file.md` |
| Convert to PDF | `pandoc path/to/file.md -o output.pdf` |
| Convert to DOCX | `pandoc path/to/file.md -o output.docx` |
| Slide to PPTX | `libreoffice --headless --convert-to pptx path/to/file.odp` |
| Image resize | `convert input.png -resize 1200x output.png` |
| Video trim | `ffmpeg -i input.mp4 -ss 00:00 -to 00:30 output.mp4` |

## Key Conventions
- Language: 繁體中文（台灣），code comments in English
- Documents: Markdown first → convert to target format on demand
- Images: `generate_image` for design, ImageMagick for processing
- Output: Deliver to `~/文件/output/{documents,designs,tools}/`
- Templates: Reuse from `~/文件/templates/`

## Document Workflow
1. Draft → Markdown in workspace
2. Review → User confirms
3. Export → pandoc/LibreOffice to target format
4. Deliver → Move to output/documents/

## Design Workflow
1. Brief → User describes need
2. Generate → `generate_image` tool
3. Refine → Iterate with feedback
4. Deliver → Move to output/designs/

## LLM & RAG Integration Conventions
- **LLM Output Correction (Faulty Markdown)**: When parsing Markdown code blocks or JSON blocks (such as `[FLEX_CARD]`) from an LLM response, always implement defensive preprocessing to normalize anomalies where backticks (\`\`\`) are outputted as commas (,,, or , , ,). Use regular expressions to replace them before executing cleans or parses.
- **RAG Semantic Cache Invalidation**: When using a semantic cache alongside a RAG knowledge base, ensure that any write operations (adds, updates, deletions, imports) in the knowledge base trigger a "semantic cache invalidation" process. Query the vector index for cached questions with similarity >= 0.85 and mark them inactive (or delete them) to preserve cache consistency.
