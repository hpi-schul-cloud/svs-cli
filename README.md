# SVS CLI

SVS is a Python command line tool for working with SVS through its HTTP APIs. It uses no database access or server internals.

## Configure

Set a backend API URL and either an access JWT or login credentials for a SchoolCloud account:

```sh
export SCHOOLCLOUD_API_URL="https://schoolcloud.example/api"
# Either provide a current JWT:
export SCHOOLCLOUD_TOKEN="your-jwt"
# Or provide credentials; the CLI logs in when needed:
export SCHOOLCLOUD_USERNAME="your-username"
export SCHOOLCLOUD_PASSWORD="provided-by-your-secret-manager"
```

The default API URL is `http://localhost:3030/api`. The token can include or omit the `Bearer ` prefix. The CLI sends it as `Authorization: Bearer …`; server API requests use `/api/v3` paths.

## Authentication and access

The v3 school list route requires JWT authentication. The controller does not apply an additional role or permission check to this list operation: the server's API test confirms that an authenticated student account can retrieve it. This differs from some individual school operations, which do perform explicit authorization checks. Use an account whose access is appropriate for the school names returned.

The server exposes these login routes under `/api/v3/authentication`:

- `POST /local` for locally managed accounts, with username and password.
- `POST /local-service-account` for locally managed service accounts, with username and password; the account must be a service account.
- `POST /ldap` for LDAP-authenticated accounts.
- `POST /oauth2` for accounts authenticated through a configured OAuth 2 identity provider.

Successful login returns an access token (JWT) used as a Bearer token for API requests. Every command now uses the same token resolver: it reuses `SCHOOLCLOUD_TOKEN` while valid, otherwise logs in with `SCHOOLCLOUD_USERNAME` and `SCHOOLCLOUD_PASSWORD`, or prompts for credentials when running interactively. Tokens obtained by logging in stay in process memory; the CLI does not save passwords or tokens to disk.

Run `auth` to log in or inspect the current account. Passwords are prompted without echo when no password is injected. `--print-token` is available for deliberate shell scripting; it writes only the token to stdout, so treat that output as secret. The login request uses the verified `/api/v3/authentication/local` route by default. Set `SCHOOLCLOUD_AUTH_METHOD=local-service-account` for a local service account. LDAP login is also supported with `SCHOOLCLOUD_AUTH_METHOD=ldap`, `SCHOOLCLOUD_SCHOOL_ID`, and `SCHOOLCLOUD_SYSTEM_ID`. OAuth/SSO providers need their provider-specific flow and cannot be assumed to accept a username and password through this CLI login. School administration prefers the separate `SVS_ADMIN_TOKEN` when set; otherwise it uses the regular token resolver. The server still checks the token's permissions on every request.

Copy `.env.example` to `.env` and set the values for your installation. Keep `.env` private; it is ignored by Git. Load it into the current shell before running commands:

```sh
cp .env.example .env
${EDITOR:-vi} .env
set -a
. ./.env
set +a
python3 svs_cli.py auth
python3 svs_cli.py rooms
```

The CLI obtains a fresh token from the environment credentials when no usable token is present. Other secret-management tools can inject the same environment variables for a command or script. For a script that needs the token value itself, use `auth --print-token` and capture stdout explicitly.

## Run from this directory

```sh
python3 svs_cli.py me
python3 svs_cli.py auth
python3 svs_cli.py me --json
python3 svs_cli.py schools
python3 svs_cli.py schools --json
python3 svs_cli.py rooms
python3 svs_cli.py rooms --json
python3 svs_cli.py rooms --name "science"
python3 svs_cli.py boards --roomid ROOM_ID
python3 svs_cli.py boards --roomid ROOM_ID --name "lesson"
python3 svs_cli.py board create --roomid ROOM_ID --name "New board"
python3 svs_cli.py board capabilities --boardid BOARD_ID
python3 svs_cli.py school systems --schoolid SCHOOL_ID
python3 svs_cli.py school system-options get --schoolid SCHOOL_ID
python3 svs_cli.py school system-options set --schoolid SCHOOL_ID --classes enabled
python3 svs_cli.py board create-link-card --boardid BOARD_ID --url "https://example.org/article"
python3 svs_cli.py board create-file-card --boardid BOARD_ID --url "https://upload.wikimedia.org/.../image.jpg"
python3 svs_cli.py board create-file-card --boardid BOARD_ID --file ./image.jpg
python3 svs_cli.py board create-content --roomid ROOM_ID --name "New lesson board" --prompt "..."
```

The default output is a table. Add `--json` to print JSON. While fetching paginated school results in table mode, the CLI updates a single progress line in the terminal; JSON mode emits no progress messages.

Or install the project in an environment:

```sh
python3 -m pip install -e .
svs schools
svs me
svs rooms
svs boards --roomid ROOM_ID
svs board create --roomid ROOM_ID --name "New board"
svs board capabilities --boardid BOARD_ID
svs school systems --schoolid SCHOOL_ID
svs school system-options get --schoolid SCHOOL_ID
svs school system-options set --schoolid SCHOOL_ID --classes enabled
```

The installed command is `svs`; `svs-cli` is also provided as an alias.

`rooms` calls authenticated `GET /api/v3/rooms` and lists rooms available to the current user. Add `--name` to filter those results by a case-insensitive name substring; the same filter applies to table and JSON output. A JWT is required; the server returns the user's room arrangement and the operations they are allowed to perform in each room.

`me` calls authenticated `GET /api/v3/me` and displays the identity, school, roles, language, and account ID associated with the current JWT. Add `--json` to print the complete response, including permissions and preferences. The token itself is never displayed; `/me` identifies the authenticated account but does not return JWT expiry details.

`boards --roomid ROOM_ID` lists boards visible to the current user in that room. Use `--name` for a case-insensitive title substring filter. Listing calls authenticated `GET /api/v3/rooms/{roomId}/boards` and requires permission to access the room's boards. Create a board with `board create --roomid ROOM_ID --name NAME`; this calls authenticated `POST /api/v3/boards` with `parentType: "room"`, the room ID as `parentId`, and `layout: "columns"`. The server requires `ROOM_EDIT_CONTENT` permission and validates board names as 1–100 characters. `--json` prints the board list or created board ID.

To create 20 empty test boards in one room, with no cards or lesson content, run:

```sh
i=1
while [ "$i" -le 20 ]; do
  svs board create --roomid ROOM_ID --name "SVS test board $i" || exit 1
  i=$((i + 1))
done
```

Each iteration creates one board through the API. The loop stops if a request fails.

`board capabilities --boardid BOARD_ID` is read-only. It combines `GET /api/v3/config/public` (deployment flags), `GET /api/v3/boards/{boardId}` and `/context` (board features and current-user permissions), and, when enabled and permitted, `GET /api/v3/tools/preferred-tools?contextType=board-element` (preferred tools for the current user's school). It lists structural actions and element types with reasons when unavailable; add `--json` for machine-readable output. Preferred tools are only the school's shortcut list; an empty list does not prove that no tools can be configured through the UI's full tool picker.

`school systems --schoolid SCHOOL_ID` lists the identity systems linked to a school via `GET /api/v3/school/{schoolId}/systems`. `school system-options get` reads provisioning settings with `GET /api/v3/schools/{schoolId}/systems/{systemId}/provisioning-options`. The CLI defaults `--schoolid` to the JWT's school and auto-selects the school's `moin.schule` system when unique; pass `--systemid` when needed. The UI currently exposes these options for `moin.schule`. Reading requires `SCHOOL_SYSTEM_VIEW`; writing requires `SCHOOL_SYSTEM_EDIT`, or `CAN_EXECUTE_INSTANCE_OPERATIONS` for instance administrators. Set one or more options with `--classes`, `--courses`, `--other-groups`, and `--external-tools`, each set to `enabled` or `disabled`. Updates are confirmed interactively; pass `--yes` to apply from a script. Disabling class, course, or other-group provisioning deletes existing groups of that type for the selected school/system, and the CLI warns before applying that change.

`board create-content` accepts either `--boardid BOARD_ID` for an existing board, or `--roomid ROOM_ID --name "Board name"` (also `--board-name`) to create a named board and then generate content for it. A newly named board is created before planning, so it remains empty if generation fails or you decline the plan. `--json` is plan-only and therefore requires an existing `--boardid`; use the named-board mode for an interactive plan or `--yes` to apply without confirmation.

Board element types, pedagogical authoring patterns, source references, and the live discovery workflow for school-dependent tools are documented in [`BOARD-CONTENT-AUTHORING.md`](BOARD-CONTENT-AUTHORING.md). The CLI's authoring playbook is [`skills/board-content-authoring/SKILL.md`](skills/board-content-authoring/SKILL.md). H5P is explicitly blacklisted for generated content. Etherpad and the built-in “new Collabora file” flow start blank; authored office content can instead be generated as a DOCX/XLSX/PPTX and uploaded as a board file, subject to File Storage accepting it and returning Collabora-editable metadata.

### Generate board lesson content

`board create-content` makes a request to a configurable LLM provider, then uses the CLI's authenticated `/api/v3` client for SchoolCloud. It creates new columns, cards, and formatted CKEditor 5 rich-text elements, applies accessible card colors when permitted, and automatically searches Wikimedia Commons for an openly licensed image and German Wikipedia for relevant source links when those board element types are available. The planning model also returns separate concise topic queries for images and Wikipedia, avoiding searches built by extracting arbitrary words from the teacher's prompt or lesson summary. The plan preview shows the queries, selected media and query used, or reports whether discovery was skipped, failed, found no results, or found no relevant results. Search results still need to match the generated topic query; a generic shared word such as “universe” alone is insufficient for a specific topic like heat death. Wikimedia `429` responses are retried according to `Retry-After` when available. Image uploads are checked for a returned FileRecord; if a Wikimedia thumbnail returns 404, the CLI retries the original image URL. If File Storage cannot fetch a Wikimedia URL and returns `FILE_NOT_FOUND`, the CLI downloads the image itself and retries through File Storage's authenticated multipart upload route. Failed uploads report attempted source URLs and remove the empty file element when permitted. The reviewed image retains author, license, and source attribution. The final summary reports uploaded images separately from links, including each link's preview status. For link previews, the CLI asks SchoolCloud to extract source metadata; if a German Wikipedia link has no preview image, it also requests the article's lead-image thumbnail through MediaWiki's PageImages API. It uploads an HTTP(S) preview image through File Storage using the same request header as the web UI, then saves a relative preview URL when File Storage marks the image previewable. Media search and creation happen automatically; there is no need to mention them in `--prompt`. The command performs live board permission and deployment-feature discovery first. It does not create drawings, Etherpad/Collabora files, H5P, external tools, or videos.

### Create a link card

`board create-link-card --boardid BOARD_ID --url URL` creates a new card in the board's first column with only a link element. Use `--columnid COLUMN_ID` to choose another existing column, and `--title` to set the card title. The command makes no LLM request. It prints metadata extraction results, source-image upload and FileRecord preview status, the preview URL it attempts to save, and the link content read back from SchoolCloud. The output is also useful for diagnosing why a preview is missing. Each run leaves its card on the board. The upload request uses the same `Embed Request User Agent` as the web UI.

`board create-file-card --boardid BOARD_ID` creates a card with one image file element. Supply exactly one of `--url URL` or `--file PATH`. URL mode tests File Storage's remote fetch and falls back to a local download plus authenticated multipart upload for Wikimedia `FILE_NOT_FOUND` responses; local-file mode uploads directly through the multipart API. Use `--columnid`, `--title`, `--caption`, or `--alt-text` as needed. The command reports the FileRecord and preview status, and removes the empty file element if the upload fails. It does not make an LLM request. Each run leaves the card on the board.

Commons image discovery requests an 800-pixel thumbnail and prefers `thumb.wikimedia.org` when Commons provides a thumbnail path. This keeps board attachments smaller and uses the thumbnail host used by successful link-preview uploads. Commons can still rate-limit downloads, so the CLI retries 429 responses before reporting a failed attachment.

Images are sourced from Wikimedia Commons rather than generated by an image model. The CLI uses Wikimedia's public APIs and SchoolCloud's authenticated File Storage upload-from-URL operation; the SchoolCloud JWT must be allowed to add files to the board. If a media write fails, the CLI reports the failed operation and server response, then continues trying the other media items; already created board content is not rolled back. To identify your installation more specifically in Wikimedia API requests, optionally set `WIKIMEDIA_USER_AGENT` to an application/version string with a contact address.

Configure the provider using environment variables only. Set `LLM_API_URL` to its full OpenAI-compatible Chat Completions endpoint, `LLM_MODEL` to a model or deployment it serves, and optionally `LLM_API_TOKEN` (or `LLM_API_KEY`) to its API token. By default, the token is sent as a Bearer token in the `Authorization` header. Set `LLM_API_KEY_HEADER` to change the header; for example, Azure API keys use `api-key` without a `Bearer` prefix. A token can be omitted for a local unauthenticated provider. SchoolCloud still uses `SCHOOLCLOUD_TOKEN` as described above.

OpenAI API example:

```sh
export LLM_API_URL="https://api.openai.com/v1/chat/completions"
export LLM_API_KEY="your-openai-api-key"
export LLM_MODEL="gpt-5.4-mini"
```

Claude API example (requires an Anthropic Console API key, **not** a Claude Pro login):

```sh
export LLM_API_URL="https://api.anthropic.com/v1/chat/completions"
export LLM_API_KEY="your-anthropic-console-api-key"
export LLM_MODEL="claude-sonnet-4-6"
```

Azure OpenAI example using the deployment-based endpoint from the Azure SDK sample:

```sh
export LLM_API_URL="https://schulcloud.openai.azure.com/openai/deployments/schulcloud-gpt-5.4-mini/chat/completions?api-version=2024-12-01-preview"
export LLM_API_KEY="your-Azure-API-key"
export LLM_API_KEY_HEADER="api-key"
export LLM_MODEL="schulcloud-gpt-5.4-mini"
```

For Azure, `LLM_MODEL` is the deployment name. The API version is part of the URL, and the key is sent in Azure's `api-key` header.

Then run the same command for either provider:

```sh
python3 svs_cli.py board create-content --boardid BOARD_ID --prompt "Create a 45-minute biology lesson on food webs for grade 7."
```

The endpoint must support the OpenAI-compatible Chat Completions request. The CLI asks for JSON and validates the returned plan locally; Anthropic's compatibility endpoint ignores the `response_format` setting, so JSON formatting is prompted and then checked by the CLI. The command prints the generated plan and asks before writing when run interactively. Answer `y` to create it; otherwise it exits without SchoolCloud writes. Add `--yes` to explicitly apply without the interactive prompt. Use `--json` to generate and print a machine-readable plan only; it never writes. The playbook and prompt are sent to the configured provider with the board title, current column titles, permission and feature status, and preferred tool names/descriptions; board IDs and credentials are not sent in the model input. Provider billing rules apply. OpenAI API use and Claude Console API use are billed separately from ChatGPT Pro and Claude Pro subscriptions, respectively. Never paste keys into prompts or commit them.

Search for a student or teacher by first and last name:

```sh
svs user search --first-name Alex --last-name Smith
svs user search --first-name Alex --last-name Smith --role student --json
svs user search --first-name Alex --last-name Smith --schoolid SCHOOL_ID
svs user search --first-name Alex --last-name Smith --school-name "Example School"
```

`--schoolid` queries only the given school ID. `--school-name` first fetches schools, matches school names case-insensitively by substring, then searches every matching school's visible user lists; if multiple schools match, all are searched. With neither option, the CLI searches teacher lists across schools and the student list for the caller's own school. This follows the room UI: teachers can be selected from other schools, while students from other schools are unavailable. It calls `GET /api/v3/school/{schoolId}/students` and `/teachers` and matches both names locally (case-insensitive substring match). Use `--role student` or `--role teacher` to limit the search; the default is both. These endpoints don't accept name filters, so the selected user lists are fetched before filtering. Global teacher search makes one request per school, with a small bounded number of requests in flight; table mode displays progress. Results remain subject to the server's school and role permissions.

Add a member to a room with a user ID (repeat `--userid` to add several):

```sh
svs room add-member --roomid ROOM_ID --userid USER_ID
svs room add-member --roomid ROOM_ID --userid USER_ID_1 --userid USER_ID_2
```

This calls `PATCH /api/v3/rooms/{roomId}/members/add` with a `userIds` array. The route requires a valid JWT. The server checks room-level `ROOM_ADD_MEMBERS` permission, or school room-administration permission when the room belongs to the caller's school. User eligibility and visibility rules may still reject an addition with `403`; the CLI reports the server's response.

## Generated API client

The server's v3 OpenAPI documentation is published at:

<https://main.niedersachsen.schulcloud-verbund.org/api/v3/docs>

The corresponding JSON spec is saved at `specs/docs-json`. That local snapshot is used by default, so client generation does not need network access when the file is present. To update it from a machine that can reach the server, run:

```sh
wget -O specs/docs-json https://main.niedersachsen.schulcloud-verbund.org/api/v3/docs-json
```

You can also let the generator fetch the latest spec with `--refresh-spec`. Then generate the Python client under `generated/server-v3/`:

```sh
python3 -m pip install -e '.[codegen]'
python3 scripts/generate_server_api.py
```

If the generated client already exists, pass `--overwrite` to regenerate it. To fetch the latest spec and regenerate in one step, use `python3 scripts/generate_server_api.py --refresh-spec --overwrite`.

Generated client code should be treated as generated output. CLI commands will provide the simpler task-oriented interface (`room add-member`, `room add-board`, and so on) on top of those API methods.

The docs URL above covers the main v3 server API. **The Admin API and Management API OpenAPI documents are not yet identified or wired into generation.** Discovering their deployed docs/spec URLs and auth schemas is follow-up work; they should be handled as separate API clients because their routes and authentication differ from v3.
