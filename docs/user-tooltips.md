# Tooltip copy deck for the web app

Version: v1, 2026-10-29. Inventory taken from `app/frontend/src` at commit `f79f664`.

This page is for contributors. It lists the controls in the web app that have no tooltip (no `title` and no `aria-label`), proposes the text for each, and records a few places where the UI's own words disagree with what the code does. The companion page for users is [user-guide.md](user-guide.md).

## How to write one

The controls that already have good tooltips (the git strip badges, the BOM export buttons, the run log levels, the dashboard sort buttons, the ActiveBoard fab status, LaunchKicad, the chat attach and Queue buttons) share a style. Keep to it:

- **Say what happens, and the consequence a new user would not guess.** "Commits and pushes to the remote; with manual push, this is the only way your work leaves the server" beats "Commit and push".
- **One sentence, two at most.** If it needs a paragraph, it belongs in the user guide; the tooltip can point there.
- **Name the file or stage involved** when the control writes one (`project.yaml`, Stage 5), because that is what the user will see in the Changes tab afterwards.
- **Disabled controls say why they are disabled.** FileView's "Edit" already does this ("This file is not one the workbench edits"); every other disabled button should too.
- **Give icon-only and placeholder-only controls an `aria-label` as well as a `title`.** A placeholder disappears when the user types and is not read reliably by screen readers.
- Phone layouts have no hover. A tooltip there is only an accessibility label; anything a phone user needs to know must be in visible text.

## Priority 1: first run, unlock and anything irreversible

These are the places a new user is most likely to lose data or get stuck.

| Where (file:line) | Control | Proposed `title` |
|---|---|---|
| `Onboarding.tsx:124` | Passphrase | Encrypts your API keys and git credentials. Not your sign-in password. The server keeps no copy and cannot reset it, so store it safely. |
| `Onboarding.tsx:131` | Confirm passphrase | Type the passphrase again; there is no reset if it is mistyped and forgotten. |
| `Onboarding.tsx:147` | Provider | The service that answers chat and resolves parts in Stage 1. You can add more later under Settings. |
| `Onboarding.tsx:160` | Base URL | Where the model server listens. Needed for local and OpenAI-compatible servers; leave empty for hosted Anthropic or OpenAI. |
| `Onboarding.tsx:173` | API key | Your own key; usage is billed to your account. Stored encrypted under your passphrase. |
| `Onboarding.tsx:185` | Model | The model this endpoint uses. Pick a suggestion or type a model id the provider offers. |
| `Onboarding.tsx:215` | Finish setup (disabled) | Fill in every field, with a passphrase of at least 10 characters typed the same twice. |
| `UnlockScreen.tsx:99` | Unlock with a passkey | Use the passkey you added on this device (Touch ID, Windows Hello or a security key). |
| `UnlockScreen.tsx:105` | Passphrase | The passphrase you chose at setup, not your sign-in password. |
| `FileTree.tsx:919` | Context menu: Delete… | Deletes and commits the delete. Files not yet in git cannot be recovered. |
| `FileTree.tsx:919` | Context menu: Remove a symbol… | Removes one symbol from this library file. Boards that use it will show a placeholder. |
| `Dialog.tsx:303` | Unsaved changes: Discard | Throw away the edits in the open file and continue. |
| `ProjectControls.tsx:272` | Commit + Push | Commits every changed file with your message, then pushes to the remote. |
| `ProjectControls.tsx:272` | Commit (no remote) | Commits every changed file. With no remote, history stays on this server only. |
| `ProjectControls.tsx:272` | Commit + Push (disabled) | Nothing to commit or push. |
| `ProjectControls.tsx:264` | Pull | Fetch and merge what others pushed to the remote. |
| `Sharing.tsx:151` | Remove (member) | Stops their future access. It cannot take back what they have already seen. |
| `Sharing.tsx:170` | Withdraw (invitation) | Cancels the invitation; the link stops working. |
| `Sharing.tsx:190` | Email input | The address they sign in with. They must have signed in here once; otherwise you get a 24-hour invitation link. |
| `ChatPanel.tsx:1686` | Allow | Let the assistant run this tool call once. Network access is asked once per session; anything that spends money or edits a board is asked every time. |
| `ChatPanel.tsx:1686` | Deny | Refuse this tool call. The assistant is told and continues without it. |
| `ProposalCard.tsx:151` | Accept | Write this change to the file and commit it. Refused if the file changed since the proposal was made. |
| `ProposalCard.tsx:151` | Reject | Discard this proposal. The file is not touched. |
| `ReleasePanel.tsx:98` | Build release package | Runs the fab gate over the Stage 8 analysis, then exports gerbers, drill, placement, BOMs and the KiCad project into one zip. |
| `ReleasePanel.tsx:110` | Download latest.zip | The newest package for this board. If the gate refused it, READ-ME-FIRST.txt inside says why. |

## Priority 2: running the pipeline and reading results

| Where (file:line) | Control | Proposed `title` |
|---|---|---|
| `StageRunner.tsx:157` | Single stage | Run one stage and inspect its artifact. Best for narrowing down a problem. |
| `StageRunner.tsx:157` | Pipeline | Run a range of stages in order, stopping at the first failure. |
| `StageRunner.tsx:157` | Review panel | Every endpoint routed to review_panel reviews the board; findings are merged with attribution. Uses your model quota. |
| `StageRunner.tsx:183` | Stage select (also needs `aria-label="Stage"`) | Which stage to run. Start with doctor after any markdown edit. |
| `StageRunner.tsx` | from / to selects (also need `aria-label`s; their `<label>`s are not associated) | First and last stage of the run. stage0 to stage8 is the whole pipeline. |
| `StageRunner.tsx` | Board scope select | One board, or every board in turn followed by the cross-board check. |
| `StageRunner.tsx` | Run / Run pipeline / Run panel (disabled during a run) | A run is already going on this project. Stop it or wait. |
| `StageRunner.tsx` | Stop | Stop the run on the server. Outputs written so far stay. |
| `RunProgress.tsx:60` | Explain the active stage | Show what the running stage does and what it writes. |
| `RunHistory.tsx:116` | Run row | Show this run's log; a running run is followed live. |
| `RunHistory.tsx:128` | Stop | Stop this run on the server. |
| `Reports.tsx:389` | Design-issue group | Show the findings in this group, with where and why. |
| `Preflight.tsx` | (tab has no controls) | Consider a short visible line: "Re-checks your markdown each time this tab opens." if that is the behaviour, or a Re-check button. |
| `BomTable.tsx:92` | Filter parts (also `aria-label="Filter parts"`) | Filter by ref, MPN, manufacturer or package. |
| `LifecycleTable.tsx:94` | Filter (also `aria-label`) | Filter by MPN or ref. |
| `LifecycleTable.tsx` | needs acknowledgement | Show only parts whose lifecycle could not be established and nobody has accepted. |
| `LifecycleTable.tsx:154` | edit | Record what you found for this part, with a reference someone else can check. |
| `LifecycleTable.tsx` | Save | Save your finding. A finding without a reference is not counted. |
| `Artifacts.tsx:61` | Artifact row | Open this pipeline output. |
| `Artifacts.tsx:107` | Open raw | Open the file as written; it is too large to render here. |
| `Artifacts.tsx:306` | Nets filter (also `aria-label`) | Filter by net name, class or refdes. |
| `DiffView.tsx:63` | File header | Show or hide this file's diff. |
| `KicadFileView.tsx:49` | Open the raw file | The KiCad file as text, in a new tab. |
| `PdfView.tsx:128` | Open PDF | Open in a new tab. Only offered for files that passed inspection. |
| `PdfView.tsx` | Download anyway | Save the file without opening it here. It did not pass inspection; open it only if you trust its source. |
| `PdfView.tsx:155` | Inspect now / Inspect again | Check the PDF for anything that acts when it is opened. |
| `FileView.tsx:365` | View | The rendered document. |
| `FileView.tsx:384` | Edit (enabled) | Edit this design document. Ctrl/⌘+S saves and commits. |
| `FileView.tsx:404` | Save | Save and commit this file (Ctrl/⌘+S). |

## Priority 3: files, projects and settings

| Where (file:line) | Control | Proposed `title` |
|---|---|---|
| `FileTree.tsx:539` | + File | New .md, .yaml or .yml file: the types the workbench edits. |
| `FileTree.tsx:539` | + Folder | New folder in the project. |
| `FileTree.tsx:539` | + Upload | Add datasheets, references or KiCad library files, up to 20 at a time. |
| `FileTree.tsx:633` | Show hidden | Show dotfiles and folders such as .blpl and .pipeline. |
| `UploadDialog.tsx:353` | Upload N files (disabled) | Choose a kind for every file first. |
| `Dashboard.tsx:79` | + Project | Start a project, clone one from git, or import files. |
| `Dashboard.tsx:142` | Project card | Open this project. An encrypted project is unsealed as it opens. |
| `Dashboard.tsx:110`, `Invitations.tsx:49` | Accept | Join this project. It appears in your list. |
| `Dashboard.tsx:110`, `Invitations.tsx:49` | Decline | Refuse this invitation. The owner can send another. |
| `InviteLanding.tsx:115` | Accept and open | Join the project and open it. The link cannot be used again. |
| `ProjectControls.tsx:522` | New / Clone git remote / Import files | Start empty / copy an existing git repository / upload files or a .zip. |
| `ProjectControls.tsx:607` | Test connection | Check that this URL and token can reach an empty repository before creating the project. |
| `ProjectControls.tsx:651` | Branch (also `aria-label="Branch"`) | Branch to clone. Leave empty for the remote's default. |
| `ProjectControls.tsx:678` | Import file input (needs a `<label>` and `aria-label`) | Markdown files, or one .zip of a project. |
| `ProjectControls.tsx:825` | Settings tabs: Board | Board outline, stackup and net classes; saved as project.yaml. |
| `ProjectControls.tsx:825` | Settings tabs: Task routing | Which model handles each task for this project only. |
| `ProjectControls.tsx:825` | Settings tabs: Git | Remote, your token, commit identity, push policy. |
| `ProjectControls.tsx:825` | Settings tabs: Library policy | Whether this project shares parts with your component library. |
| `ProjectControls.tsx:825` | Settings tabs: Auto-lock | How long the project stays open while nobody touches it. |
| `BoardConfigPanel.tsx:371` | Copper layers | Layer count of the board you will order. Changes which layers keepouts and planes may use. |
| `BoardConfigPanel.tsx:371` | Thickness (mm) | Finished board thickness; 1.6 is standard. |
| `BoardConfigPanel.tsx:371` | Finish | Surface finish. ENIG suits fine-pitch parts; HASL is cheapest. |
| `BoardConfigPanel.tsx:303` | Width / Height (editable) | Board size in mm. Must match the outline. |
| `BoardConfigPanel.tsx` | Follows part (refdes) | Fix this shape to a part so it moves with it, as an antenna keepout should. Empty keeps it fixed on the board. |
| `BoardConfigPanel.tsx` | Forbid checkboxes | What may not enter this area: tracks, vias, pads, copper pours, footprints. |
| `BoardConfigPanel.tsx:433` | Save board config | Writes project.yaml. Re-run Stage 5 to rebuild the board with it. |
| `ImpedanceFields.tsx` | Add net class | Add a class with its own trace width and clearance. Nets join it by name rule or impedance tag. |
| `ImpedanceFields.tsx` | Add impedance tag | Mark nets, by name pattern, as needing a controlled impedance. |
| `ProjectRoutingPanel.tsx` | Save routing | Applies to runs and chats started from now on in this project. |
| `IdleSealPanel.tsx:82` | Save auto-lock | The project is encrypted again after this many idle minutes, never during a run. |
| `LibraryPolicy.tsx:63` | Radio options | (The option text is already explanatory; add `title` only if the text is shortened.) |
| `Settings.tsx:77` | Section tabs | One title per tab, matching the user guide's Settings table. |
| `Settings.tsx:230` | + Endpoint | Add a named model connection. Tasks are routed to endpoints by name. |
| `Settings.tsx` | list models | Ask this endpoint which models it offers. |
| `Settings.tsx` | Remove (endpoint) | Remove this endpoint and its stored key. Tasks routed only to it will need another. |
| `Settings.tsx:730` | reset to default | Restore this task's endpoint order to the default. |
| `Settings.tsx:934` | Git presets | Fill in the host for this service. |
| `Settings.tsx:828` | Name this device | A label for this passkey, so you can tell devices apart when removing one. |
| `Settings.tsx:835` | Add a passkey | Unlock with Touch ID, Windows Hello or a security key instead of typing your passphrase. |
| `NetworkAccess.tsx:157` | Also git over ssh | Allow git over SSH (port 22) to this host as well as HTTPS. |

## Priority 4: chat and navigation details

| Where (file:line) | Control | Proposed `title` |
|---|---|---|
| `ChatPanel.tsx:1425` | Message box (needs `aria-label="Message"`) | Enter sends; Shift+Enter for a new line; / for commands. |
| `ChatPanel.tsx:1519` | Send | Send this message (Enter). |
| `ChatPanel.tsx:1514` | Stop | Stop this answer. Queued messages are held, not sent. |
| `ChatPanel.tsx:1378` | Send now | Send the held message now. |
| `ChatPanel.tsx:1378` | Discard | Drop every held message. |
| `ChatPanel.tsx:1101` | Ask again | Send the same question again. |
| `ChatPanel.tsx:1274` | Jump to new | Scroll to the newest message (or the approval waiting for you). |
| `ChatPanel.tsx:1164` | Conversation select | Switch to another conversation in this project. |
| `ChatPanel.tsx` | New session | Start an empty conversation. The current one is kept. |
| `SlashCommands.tsx:124` | Slash items | (Description already shown inline; add `title` equal to the inserted text.) |
| `AskChatButton.tsx:29` | Ask in current / new conversation | Add the reference to this conversation / start a new one with it. Nothing is sent until you press Send. |
| `TabBar.tsx:93` | Viewer tabs | One sentence per tab, from the "Viewer tabs" table in the user guide. |
| `Rail.tsx:43` | Section headers | Show or hide this section. A hidden section stops refreshing. |
| `App.tsx:597` | Phone tabs Files / View / Chat / More | `aria-label` only (no hover on a phone). |
| `App.tsx:169` | Project picker | Switch project. ⚠ marks one with a problem. |
| `BoardPanel.tsx:84` | Board button with no note | Make this the active board. |

## Inconsistencies found while taking the inventory

These are small, but a new user reads UI text literally.

1. **Stage 6a says "Freerouting".** `stages.ts:74` summarises stage6a as "Autoroutes the emitted board with Freerouting", but `routing.router` in `project.yaml` defaults to KiCad Routing Tools when it is installed. Suggest "Autoroutes the emitted board (KiCad Routing Tools or Freerouting)".
2. **"Account and Auth page".** `Onboarding.tsx:122` tells the user a login password is set on the "Account and Auth" page. No screen has that name; the nearest thing is Clerk's account menu under the avatar. Suggest naming it as the user sees it: "your account menu (your avatar, top right)".
3. **`init` has no description.** It is in the Single stage list, but `STAGE_INFO` has no entry, so "Explain the active stage" shows "No description for init." Suggest: summary "Writes project.yaml from your markdown", detail "Reads the identity, stackup and net-class tables in your design documents and writes project.yaml, which Stage 5 needs. Existing files are kept unless forced."
4. **Stage 7 summary omits ERC.** `STAGE_INFO.stage7` lists KLC, DRC and coverage; Stage 7 also runs ERC (and the Reports tab shows an ERC pill).
5. **The release log is unclassified.** The Release tab's log is a raw `<pre>`, while run logs have Milestones / Normal / Everything. A user who learns the run log will look for the same control here.
6. **Upload limit wording.** The dialog says "Any type, up to 20 at a time"; the frontend enforces no limit and the server decides. Worth checking the server limit matches the text.
