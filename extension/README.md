# InfoGrep Form Filler (Chrome extension)

A side panel that fills in the form on the page you have open, from your own files. Press
**Fill in this page**: the panel reads the page's fields, the local InfoGrep server has a
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) agent look each value up in
your indexed files, and the panel types the answers in. It lists what it filled and the file
each value came from, and what it could not fill.

It only fills fields in. It never presses a button and never submits the page: you check the
form and submit it yourself.

## What it fills

- Native fields: text, email, phone, number, date, text areas, lists, radio buttons and
  tick boxes, in every frame of the page.
- Dropdowns and cascaders that pages draw themselves: Element UI, Element Plus and Ant
  Design, and others that follow the ARIA combobox pattern, such as Material UI's Select.
  It opens one, finds the choice (searching it, or scrolling a long list), and clicks it.
- Rich-text editors: Quill, CKEditor 5, TinyMCE, wangEditor and plain editable boxes.
  These take prose only when it is copied word for word from your files (see below).
- Fields that a choice brings in. A filled-in form can change: a province brings its
  cities, and a "yes" brings the fields that ask for details. So a fill goes in up to three
  rounds. After each round, the panel waits for the page to settle, tries again any list
  whose choices arrived late, and asks the agent about the fields that came up, or that now
  offer other choices.

The only clicks it makes are on a tick box or radio button that it sets, and on a
dropdown: the dropdown's own box, and a choice in the list that box opened. Never on a
link, a button that could submit, or anything outside the dropdown being filled.

## Prose in rich-text boxes

A rich-text box asks for prose, such as a statement, an abstract or a list of awards. The
agent is told to copy such text from a file, never to write it, and the server checks: the
passage goes in only if the file it names has it word for word (spacing and line breaks
aside). Copied prose is marked in the panel so you read it before you submit. A passage the
file doesn't have is shown with a note and not typed in, so you can use it yourself if it
is right.

The panel also checks that an editor really took the text into its own model, not just
onto the screen. Otherwise a box can look filled in and still send nothing when you submit.

## Install

1. Have the InfoGrep server running (the menu-bar app runs it, or `infogrep serve`), with
   `dsh` installed and a model configured. See
   [Set up question answering](../README.md#set-up-question-answering-deepseek-harness): if
   **Ask** works, this does too.
2. Get the extension: this `extension/` folder in a checkout of InfoGrep, or the
   `infogrep-form-filler-<version>.zip` attached to each
   [release](https://github.com/QingyaoAi/InfoGrep/releases), unzipped. Then open
   `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and pick that
   folder.
3. Pin the InfoGrep icon to the toolbar. Clicking it opens the side panel.

Needs Chrome 121 or later.

## Use

- **Fill in this page** fills the empty fields of the page in front of you, in every frame
  of it. It takes a while: about half a minute for a short form. Click a row in the results
  to bring that field into view, and click a file name to show the file in its folder.
- **Note for the agent** is sent with every fill and kept for next time, for things the files
  won't say: who the form is for, which of two addresses to use, "English, not Chinese".
- **Search** narrows the agent to one indexed folder (say, your personal-documents folder),
  which is faster and less likely to pick up someone else's details.
- **Replace what is already filled in**: off by default, so what you, or the site, typed
  is kept. The agent's suggestion for a filled field is still listed.
- The next fill goes on with the same agent conversation, so on the next page of a
  multi-page form, or on a second form, the agent already knows what it found.
  **Start over** forgets it, and so does changing the folder. Moving on to the next page is
  up to you: a "Next" button often saves the page to the site, and the panel never presses
  one.

## What it remembers

The facts about you that the agent finds (your name, phone, employer, title, ID numbers
and so on, each with the file it came from) are remembered for the next form, so that it
needn't search for them again. On the form used for testing, the first fill took about a
minute, and a second one made no searches and took 15–20 seconds.

- **A note you can edit.** The facts are kept in `Agent Knowledge/表单记忆.md` in your
  Obsidian vault (the one your knowledge base reads), or, with no vault, in
  `~/.infogrep/ask/fill/`. The panel says how many there are and opens the note. Change a
  line and it counts as yours: a file that says otherwise later doesn't overwrite it, and
  the note shows what the file says beside it. Add a line and it is remembered too, and
  delete one to forget it. Facts are kept in two sections, **不常变** (things that don't
  change over time) and **会变** (things that do); move a line to change which one it is in.
- **Checked again when it may be out of date.** A fact goes back to the agent to be checked
  when the file it came from changes or is gone. A fact in **会变** is checked again too
  when files changed since then mention it, or when it was last checked half a year ago.
- **Sensitive numbers stay out of the prompt.** ID, passport, bank-card and similar
  numbers are not sent to the model with each form: the agent is told only that they are
  remembered, and the InfoGrep server puts the value into the field the agent names. They
  are marked 不发给模型 in the note.
- **Not for other people's forms.** Untick *Use what is remembered about me* for a form
  that is about someone else: nothing remembered is used, and nothing is learned from it.

InfoGrep's own record of the facts (when each was checked, the state of its file, and what
InfoGrep last wrote, which is how your edits show) is `~/.infogrep/ask/fill/memory.json`,
readable only by you.

## What leaves your machine

Only when you press the button: the page's title and address (without its query string),
the label, hint and options of each field, and what is already in the fields. All of it
goes to your local InfoGrep server. The agent then sends that, plus the parts of your files
it reads, to the model `dsh` uses, as Ask does, and so do the remembered facts, except the
sensitive numbers above. The agent's session log is not also uploaded to DeepSeek's API.
These conversations are kept under `~/.infogrep/ask/fill/` and deleted a week after their
last use.

Password fields, hidden fields, and fields a page marks as card details or one-time codes
are never read or filled. The agent is told never to guess, and never to fill in
verification codes.

## What it doesn't fill

- File uploads. Many upload boxes send the file to the site the moment it is chosen, long
  before you submit, so choosing one is for you to do.
- Captchas and verification codes. They are there to tell a person from a program.
- Widgets drawn some other way than the libraries above, with nothing ARIA about them, and
  the date pickers of those libraries (their text box is filled in like any other, and
  that is untested).

A page that submits itself when a field changes still does so. That is rare on real forms,
but it can happen on a search or filter page.

## Permissions

- `sidePanel`: the panel itself.
- `scripting` with access to all sites: to read a page's fields and fill them in, on
  whatever site the form is on, when you press the button. Nothing runs in any page before
  that.
- `storage`: your note, the folder you picked and the server address.

## How it fits together

| File | Does |
|---|---|
| `sidepanel.html`, `.css`, `.js` | The panel. It talks to the InfoGrep server (`POST /api/fill`, then polls `GET /api/fill?id=`). |
| `page.js` | Injected into each frame of the page when you press **Fill** or rescan. Lists the fields and fills them in. |
| `background.js` | Opens the panel when the toolbar icon is clicked. |
| [`../infogrep/fill.py`](../infogrep/fill.py) | On the server: what the agent is told, and which parts of its reply are used. |
