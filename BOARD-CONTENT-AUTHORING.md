# Board content authoring notes

This is a retained research note for a future prompt-driven `board create-content` command. It records the board model and the content choices verified in the current SchoolCloud server and Nuxt client sources. It does not create API data or define a JSON/YAML fixture format.

The read-only CLI command `board capabilities --boardid BOARD_ID` now performs the deployment, board, permission, and preferred-tool checks described below and prints the currently available options. It is a capability inventory, not yet a prompt-to-content generator.

## What a board contains

A column board is organized as:

```text
board -> columns -> cards -> content elements
```

Columns and cards provide the layout. Content elements inside a card carry the learning material or activity. One card can contain more than one element. The server also permits creating a card with a `requiredEmptyElements` array, which is how the UI can reserve an empty element slot, but a content authoring flow should create only elements it intends to use.

## Element options found in the UI and server

The server enum is `ContentElementType` in `schulcloud-server/apps/server/src/modules/board/domain/types/content-element-type.enum.ts`; the UI picker and its feature checks are in `nuxt-client/src/modules/feature/board/shared/AddElementDialog.composable.ts`.

| Type | What it can do in a lesson | Content and runtime considerations |
| --- | --- | --- |
| `richText` | Explain a concept, give instructions, present a worked example, or add a reflection prompt. | Supports formatted HTML in `text` with `inputFormat: richTextCk5`. The server sanitizer allows paragraphs, headings, emphasis, ordered/unordered lists, tables, and other CKEditor tags. The CLI should use semantic `<p>`, `<ul>/<ol>`, and `<li>` markup rather than Markdown-like hyphen lines. See `board/controller/dto/element/update-element-content.body.params.ts` and `shared/controller/transformer/sanitize-html.transformer.ts`. |
| `drawing` | Collaborative whiteboard for a concept map, model of a cell, food web, labeled diagram, or group hypothesis. | The content DTO has a `description`; actual drawing is an interactive tldraw experience. UI creation is gated by `FEATURE_TLDRAW_ENABLED`, and collaborative editing also depends on board collaboration/socket configuration. Do not try to serialize the drawing as ordinary text. |
| `collaborativeTextEditor` | Blank shared Etherpad workspace for group notes, a lab protocol, an argument, or a joint explanation. | It has no ordinary text content payload in the REST response. It represents a collaborative editor provided by a separate service; the UI feature flag is `FEATURE_COLUMN_BOARD_COLLABORATIVE_TEXT_EDITOR_ENABLED`. Always create it blank. Put task directions in a separate `richText` element; never seed text into the pad. |
| `link` | Point to an approved source, simulation, video, or external reading. | Holds a URL and optional title, description, and image metadata. The board link feature is controlled by `FEATURE_COLUMN_BOARD_LINK_ELEMENT_ENABLED`. The UI calls authenticated `POST /api/v3/meta-tag-extractor` to discover preview metadata; if an Open Graph image is present, it attaches a File Storage record to the link element and stores the generated preview URL in `imageUrl`. |
| `file` | Attach a worksheet, image, dataset, or other file; a compatible Office document may be editable in Collabora. | Create the file element through the board API, then upload its binary through the File Storage API with the element as parent (`parentType: boardnodes`). Besides multipart upload, File Storage supports authenticated `POST /api/v3/file/upload-from-url/{storageLocation}/{storageLocationId}/{parentType}/{parentId}` with a JSON `url`, `fileName`, and optional fetch headers. This is the same flow the UI uses for link-preview images and can fetch a Wikimedia Commons image directly. Check the returned `previewStatus`; preview generation may be delayed or unavailable. For DOCX/XLSX/PPTX, the resulting FileRecord's `isCollaboraEditable` controls whether the UI exposes Collabora. |
| `fileFolder` | Gather files for a project or lab. | Holds a title and is gated by `FEATURE_COLUMN_BOARD_FILE_FOLDER_ENABLED`. Useful for collecting student submissions or shared resources; actual file operations are a separate workflow. |
| `h5p` | Blacklisted for prompt-driven board authoring. | The platform may expose this type and the element references an existing H5P `contentId`, but the authoring policy is to never create, select, recommend, or substitute H5P content, even when the feature flag is enabled. |
| `externalTool` | Embed a school-provided interactive simulation, assessment, or learning app. | The element refers to a `contextExternalToolId`. Availability is deployment-, school-, context-, and permission-dependent; discover live for the authenticated user. Never guess tool IDs or parameters. |
| `videoConference` | Schedule a synchronous discussion, tutoring session, or group check-in. | Has a title. The UI requires the global and board video-conference feature flags, the `videoconference` board feature, and the `manageVideoConference` allowed operation. Do not create one unless the prompt calls for synchronous work and access is available. |
| `file` via Collabora | Students or teachers can open and edit an attached Office document. | This is not a separate `ContentElementType`. The built-in “new Collabora file” picker creates a blank office document using File Storage `addDocumentToParent` (filename and document type only). Separately, the UI's ordinary file upload flow can upload a locally generated DOCX/XLSX/PPTX to a generic file element; the returned `isCollaboraEditable` flag determines whether the board UI exposes Collabora, and preview depends on separate preview metadata. Thus authored Office content can be supplied without UI by generating a valid Office Open XML file and using the documented binary upload endpoint, then checking the resulting FileRecord. File Storage is separate from the main server API; derive its base URL/config from service configuration and verify current auth, permissions, supported MIME types, size limits, scan status, and preview behavior before implementing. Do not claim editability or previewability until confirmed by the response. Etherpad remains blank-only. |
| `deleted` | None for new learning content. | This is a tombstone/response type, not an authoring option. Never request it when creating a card. |

The content enum in the local OpenAPI snapshot also includes every value above except Collabora, which is a separate UI flow. The API's generic element creation route accepts a type, but successful rendering alone does not prove that all server/client feature switches or supporting services are enabled.

## Pedagogical patterns worth offering

Use the user's subject, age group, learning goal, available lesson time, and class setup to choose among these. Biology examples are starting points, not fixed templates.

- **Concept explanation:** short rich-text explanation, followed by a source link or labeled file.
- **Explore and model:** prompt in rich text, then a drawing element for a cell, ecosystem, food web, or process diagram. Give teams a concrete artifact and a legend/labeling expectation.
- **Group investigation:** define roles and a question in rich text, use Etherpad for shared observations, and finish with a drawing/model or group explanation.
- **Claim, evidence, reasoning:** one card with a prompt, evidence collection instructions, and a rich-text response area; optionally add Etherpad for co-authored work.
- **Retrieval practice:** use text prompts and a simple response scaffold. H5P is blacklisted for this authoring workflow, even if available on the deployment; don't imply automatic grading.
- **Compare and classify:** columns can represent categories or stages; cards can ask students to classify examples, link evidence, or explain a choice.
- **Synchronous discussion:** video conference only when requested and enabled; include a clear question or facilitation task.
- **Resource collection:** file folder for student or teacher resources, with a rich-text card describing acceptable formats and naming conventions.

Prefer a small number of purposeful columns and cards. Make instructions observable (what learners produce, compare, label, or explain), include accessibility-aware text, and avoid assuming that every student can use an external tool or has a file/device.

The planning model supplies separate concise search queries for Wikimedia Commons illustrations and German Wikipedia sources. Build them from the actual topic, not stray words from the teacher's sentence, lesson instructions, age, or duration. The interactive plan review displays the queries and results, so the user can catch a poor query before any write. Search results must still have meaningful topical overlap with the model-proposed query; a generic shared word such as “universe” does not make an astronomy image relevant to the heat-death theory. The CLI filters Commons results to CC0, public-domain, CC BY, and CC BY-SA licenses, excluding noncommercial and no-derivatives licenses. If there is no relevant eligible result, omit the image rather than choose a weak match. Retry Wikimedia API `429` responses using `Retry-After` when available. Before any write, the interactive plan review shows the image title, author, license, source page, and selected article URLs. Upload the Commons thumbnail through File Storage's authenticated upload-from-URL operation; if that remote fetch returns `FILE_NOT_FOUND`, download the Wikimedia image in the CLI and use File Storage's authenticated multipart upload endpoint. Retain author, license, and source URL in the file caption, and require a returned FileRecord ID before reporting it attached. If upload fails, report the attempted source URL(s) and remove the empty file element when permitted. Link elements use the server meta-tag extractor; when a German Wikipedia page has no extracted preview image, use the MediaWiki PageImages API to try its lead-image thumbnail. Upload HTTP(S) preview images to File Storage, subject to returned preview status. Save the resulting `imageUrl` as a relative preview path: the board server ignores absolute image URLs even though it accepts the link's source URL. If no eligible image/article is found or the element is unavailable, continue with text only. Color cards from a small accessible palette when `allowedOperations.updateCardColor` is true.

## Discovery when a prompt is actually run

This workspace's source research is a useful baseline, not a substitute for checking the live deployment. Availability is partly controlled by public deployment config and partly by the current user's school, room, permissions, and tool installations.

Before proposing content for a real board, use read-only `/api/v3` calls with the user's token to:

1. Fetch `GET /api/v3/config/public`. This is the deployment feature-flag source the Nuxt client loads through `serverConfigControllerPublicConfig()` in `nuxt-client/src/modules/data/env/env-config.store.ts`. Inspect board flags such as `FEATURE_COLUMN_BOARD_*`, `FEATURE_TLDRAW_ENABLED`, `FEATURE_VIDEOCONFERENCE_ENABLED`, and `FEATURE_PREFERRED_CTL_TOOLS_ENABLED`. These flags answer whether the UI exposes feature families; they do not establish the user's permission to use them.
2. Fetch `GET /api/v3/boards/{boardId}` and, when needed, `GET /api/v3/boards/{boardId}/context`. Check the returned layout, board `features`, columns, and `allowedOperations` before planning writes. For a room board, compare its context to the supplied room ID and honor `createColumn`, `createCard`, and element-specific permissions. The per-board response answers questions that deployment-wide flags cannot.
3. Fetch `GET /api/v3/tools/preferred-tools?contextType=board-element`. This is authenticated and scoped by the server to the current user's school and preferred tools available for that context. Treat returned names and IDs as discovery data; only use an ID after the user selects the tool or the plan clearly calls for it.
4. If no preferred tool fits, inspect other school tools only through documented, authorized API routes. `GET /api/v3/tools/school/{schoolId}/available-tools` can be permission-restricted; a `403` means stop that discovery path and report what was unavailable. Do not use admin endpoints, scrape the browser, or infer tools from a different deployment.
5. For a selected external tool, inspect the context-specific configuration template before attaching it. The UI flow creates an `externalTool` element, retrieves `GET /api/v3/tools/board-element/{elementId}/available-tools`, matches the selected `schoolExternalToolId`, and creates/updates a context external tool with its required parameters. If it needs configuration, present the required choices to the user; never fill school-specific parameters by guessing.
6. If the plan needs file upload/Collabora or another separate service, discover its public configuration only when that capability is in scope. The Nuxt client loads an optional File Storage service config separately; it is not covered by `/api/v3/config/public`. For authored Office documents, create a `file` element, upload the generated binary as its `BOARDNODES` child through File Storage, and inspect the returned FileRecord for `isCollaboraEditable` and preview metadata. Do not route File Storage calls through the server `/api/v3` base URL unless live service configuration confirms that is correct.
7. Summarize what is available and propose the board structure before writing. Clearly mark unavailable feature types and distinguish installed external tools from generic content possibilities. Mark H5P as policy-blacklisted regardless of technical availability. If a required tool isn't available, offer a pedagogically equivalent rich-text, drawing, or blank Etherpad alternative.

No live API discovery was performed while writing this note. The list above is the live preflight to perform when a real prompt is run.

For a complete picture, treat discovery as the combination of (a) deployment-wide Nuxt feature flags, (b) target board context/features and user-specific allowed operations, and (c) school/context-specific external tool listings. None of these alone is sufficient. Add specialized service configuration only when the proposed content actually uses that service.

## API shape and verified source trail

The API paths below are all in the checked-in `specs/docs-json` snapshot and are served under `/api/v3`:

- `GET /rooms/{roomId}/boards`: room board list. `RoomController.getRoomBoards` checks that the room is not locked and the user can `accessRoomBoards`; boards are filtered by board-level `findBoard` permission. See `schulcloud-server/apps/server/src/modules/room/api/room.controller.ts` and `room-content.uc.ts`.
- `GET /boards/{boardId}` and `GET /boards/{boardId}/context`: inspect a board and its context. JWT required; board-level access is enforced in `BoardUc`.
- `POST /boards/{boardId}/columns`: create a column (no body). `PATCH /columns/{columnId}/title` can rename it.
- `POST /columns/{columnId}/cards`: create a card; optional `requiredEmptyElements` and `position`.
- `POST /cards/{cardId}/elements`: create one of the supported element types.
- `PATCH /elements/{contentElementId}/content`: update supported typed content. This isn't a substitute for the UI's specialized upload, editor, drawing, or tool-configuration flows.
- `GET /tools/preferred-tools?contextType=board-element` and `GET /tools/{contextType}/{contextId}/available-tools`: authenticated tool discovery/configuration routes.
- `POST /tools/context-external-tools`: create a context-specific tool instance. It requires a school tool ID, context ID/type, and may need configured parameters.

The board, column, card, and element controllers use JWT authentication. API schema presence is not a permission grant: each operation runs board/room authorization checks and may return `403`. The UI's `allowedOperations` and public feature switches should inform a plan, while the server remains authoritative.

## How this was explored

The inventory was built by cross-checking:

1. The local OpenAPI snapshot at `svs-cli/specs/docs-json` for route methods, request/response shapes, bearer security, content-element enum values, and public config fields. The snapshot URL and refresh command are recorded in `README.md`.
2. `schulcloud-server/apps/server/src/modules/board/controller` and its DTOs for validation, actual route behavior, auth decorators, and type-specific content fields.
3. `schulcloud-server/apps/server/src/modules/board/uc` and `schulcloud-server/apps/server/src/modules/room/api/room-content.uc.ts` for authorization checks, feature/service coupling, and room visibility behavior.
4. `nuxt-client/src/modules/feature/board/shared/AddElementDialog.composable.ts`, `card/ContentElementList.vue`, board/card stores, and `nuxt-client/src/modules/data/external-tool/contextExternalToolApi.composable.ts` for options actually offered by the UI, deployment flags, rendering, and school-specific preferred-tool setup.

When server or client code changes, repeat these local checks against the pinned spec and sources. When the command is used in a live school, repeat the read-only deployment preflight above because feature flags and tools vary by installation.
