/**
 * Settings dialog.
 *
 * Follows the DeepSeek Harness pattern: a centred modal with a left nav rail,
 * one row per preference (label + description on the left, control on the
 * right), and **auto-save on change** — no Save button for ordinary rows.
 *
 * The security rule that shapes this file: the server never sends a credential
 * value. A key is described only by `configured` / `source` / `writable`, so the
 * input is write-only. Its placeholder changes with the state, and the field is
 * disabled when the value comes from the environment, because saving over that
 * would silently do nothing.
 */

const SECTION_LABELS = {
  model: "Model",
  agent: "Agent",
  voice: "Voice",
  general: "General",
};

const SECTION_ORDER = ["model", "agent", "voice", "general"];

export class SettingsPanel {
  constructor({ onSaved, onToast }) {
    this.onSaved = onSaved;
    this.onToast = onToast;
    this.described = null;
    this.activeSection = "model";
    this.pending = new Set();

    this.modal = document.getElementById("settingsModal");
    this.nav = document.getElementById("settingsNav");
    this.body = document.getElementById("settingsBody");
    this.title = document.getElementById("settingsSectionTitle");

    document.getElementById("settingsClose").addEventListener("click", () => this.close());
    document.getElementById("settingsMask").addEventListener("click", () => this.close());
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.modal.hidden) {
        event.preventDefault();
        this.close();
      }
    });
  }

  get isOpen() {
    return !this.modal.hidden;
  }

  async open() {
    this.modal.hidden = false;
    await this.reload();
    document.getElementById("settingsClose").focus();
  }

  close() {
    this.modal.hidden = true;
    document.getElementById("settingsOpen").focus();
  }

  async reload() {
    const response = await fetch("/api/settings");
    this.described = await response.json();
    this.renderNav();
    this.render();
  }

  renderNav() {
    const sections = Object.keys(this.described.sections || {});
    const ordered = SECTION_ORDER.filter((name) => sections.includes(name)).concat(
      sections.filter((name) => !SECTION_ORDER.includes(name)),
    );

    this.nav.replaceChildren();
    for (const section of [...ordered, "providers"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "modal__navItem";
      button.textContent = section === "providers" ? "API keys" : SECTION_LABELS[section] || section;
      button.setAttribute("aria-current", String(this.activeSection === section));
      button.addEventListener("click", () => {
        this.activeSection = section;
        this.renderNav();
        this.render();
      });
      this.nav.append(button);
    }
  }

  render() {
    this.title.textContent =
      this.activeSection === "providers"
        ? "API keys"
        : SECTION_LABELS[this.activeSection] || this.activeSection;
    this.body.replaceChildren();

    if (this.activeSection === "providers") {
      this.renderProviders();
    } else {
      this.renderFields(this.described.sections[this.activeSection] || []);
    }
  }

  renderFields(fields) {
    for (const field of fields) {
      const row = document.createElement("div");
      row.className = "setting";

      const text = document.createElement("div");
      text.className = "setting__text";

      const label = document.createElement("label");
      label.className = "setting__title";
      label.textContent = field.label;
      const controlId = `setting-${field.name}`;
      label.htmlFor = controlId;

      const desc = document.createElement("span");
      desc.className = "setting__desc";
      desc.textContent = field.help;
      if (field.env_locked) {
        desc.textContent += " Currently set by an environment variable, so this is read-only here.";
      }
      text.append(label, desc);

      const control = document.createElement("div");
      control.className = "setting__control";
      control.append(this.buildControl(field, controlId));

      row.append(text, control);
      this.body.append(row);

      const error = document.createElement("p");
      error.className = "error";
      error.hidden = true;
      error.id = `${controlId}-error`;
      this.body.append(error);
    }
  }

  buildControl(field, controlId) {
    const disabled = field.env_locked || this.pending.has(field.name);

    if (field.kind === "bool") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.id = controlId;
      input.checked = Boolean(field.value);
      input.disabled = disabled;
      input.addEventListener("change", () => this.save(field.name, input.checked, controlId));
      return input;
    }

    if (Array.isArray(field.choices) && field.choices.length > 0) {
      const select = document.createElement("select");
      select.className = "select";
      select.id = controlId;
      select.disabled = disabled;
      for (const choice of field.choices) {
        const option = document.createElement("option");
        option.value = String(choice);
        option.textContent = String(choice);
        option.selected = String(field.value) === String(choice);
        select.append(option);
      }
      select.addEventListener("change", () => this.save(field.name, select.value, controlId));
      return select;
    }

    const input = document.createElement("input");
    input.className = "input";
    input.id = controlId;
    input.disabled = disabled;
    if (field.kind === "float") {
      input.type = "number";
      input.step = "0.1";
      if (field.minimum !== null && field.minimum !== undefined) input.min = field.minimum;
      if (field.maximum !== null && field.maximum !== undefined) input.max = field.maximum;
    } else if (field.kind === "int") {
      input.type = "number";
      input.step = "1";
      if (field.minimum !== null && field.minimum !== undefined) input.min = field.minimum;
      if (field.maximum !== null && field.maximum !== undefined) input.max = field.maximum;
    } else {
      input.type = "text";
    }
    input.value = field.value ?? "";

    // Commit on blur and Enter rather than every keystroke: each save rebuilds
    // the model client, so per-character writes would be wasteful.
    const commit = () => this.save(field.name, input.value, controlId);
    input.addEventListener("change", commit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        commit();
      }
    });
    return input;
  }

  async save(name, value, controlId) {
    const error = document.getElementById(`${controlId}-error`);
    if (error) {
      error.hidden = true;
      error.textContent = "";
    }
    this.pending.add(name);

    try {
      const response = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [name]: value }),
      });
      const payload = await response.json();

      if (!response.ok) {
        if (error) {
          error.textContent = payload.error || "That value was rejected.";
          error.hidden = false;
        }
        const input = document.getElementById(controlId);
        if (input) input.setAttribute("aria-invalid", "true");
        if (this.onToast) this.onToast(payload.error || "That value was rejected.", "error");
        return;
      }

      this.described = payload;
      this.renderNav();
      this.render();
      if (this.onSaved) this.onSaved(payload);
      if (this.onToast) this.onToast("Saved.", "ok");
    } catch (cause) {
      if (this.onToast) this.onToast(`Could not save: ${cause.message}`, "error");
    } finally {
      this.pending.delete(name);
    }
  }

  renderProviders() {
    for (const provider of this.described.providers || []) {
      this.body.append(this.buildProviderCard(provider));
    }

    const paths = document.createElement("div");
    paths.className = "detail";
    paths.style.marginTop = "16px";
    paths.append(
      keyValue("Preferences file", this.described.settings_path),
      keyValue("Credentials file", `${this.described.credentials_path} (owner-only)`),
      keyValue("Data directory", this.described.data_dir),
    );
    this.body.append(paths);

    const note = document.createElement("p");
    note.className = "notice";
    note.style.marginTop = "12px";
    note.textContent =
      "Keys are stored locally with owner-only permissions and are never sent back to the " +
      "browser. A key already present in your environment or .env file takes precedence, so " +
      "it shows as read-only here.";
    this.body.append(note);
  }

  buildProviderCard(provider) {
    const credential = provider.credential || {};
    const card = document.createElement("div");
    card.className = "provider";

    const head = document.createElement("div");
    head.className = "provider__head";

    const dot = document.createElement("span");
    dot.className = `provider__dot ${credential.configured ? "provider__dot--on" : "provider__dot--off"}`;
    dot.setAttribute("role", "img");
    dot.setAttribute(
      "aria-label",
      credential.configured ? "API key configured" : "API key missing",
    );

    const name = document.createElement("span");
    name.className = "provider__name";
    name.textContent = provider.label;

    head.append(dot, name);

    if (credential.source === "env") {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "from environment";
      head.append(badge);
    } else if (credential.source === "file") {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "saved locally";
      head.append(badge);
    }

    card.append(head);

    const body = document.createElement("div");
    body.className = "provider__body";

    const label = document.createElement("label");
    label.className = "setting__title";
    label.textContent = `${provider.api_key_env}`;
    label.htmlFor = `cred-${provider.id}`;

    const input = document.createElement("input");
    input.className = "input";
    input.id = `cred-${provider.id}`;
    input.type = "password";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.disabled = credential.writable === false;
    input.placeholder = credential.configured
      ? credential.writable === false
        ? "Provided by the environment (read-only)"
        : "Configured — enter a new value to replace"
      : "Paste your API key";

    const error = document.createElement("p");
    error.className = "error";
    error.hidden = true;

    const actions = document.createElement("div");
    actions.className = "provider__actions";

    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.className = "button button--primary";
    saveButton.textContent = "Save key";
    saveButton.disabled = credential.writable === false;
    saveButton.addEventListener("click", () =>
      this.saveCredential(provider, input, error, saveButton),
    );

    const verifyButton = document.createElement("button");
    verifyButton.type = "button";
    verifyButton.className = "button button--ghost";
    verifyButton.textContent = "Test";
    verifyButton.disabled = !credential.configured && credential.writable === false;
    verifyButton.title = credential.configured
      ? "Check this key against the provider"
      : "Check the key you typed, without saving it";
    verifyButton.addEventListener("click", () =>
      this.verifyCredential(provider, input, verifyButton, error),
    );

    // Typing a key enables testing it; clearing the field falls back to the
    // stored key, if there is one.
    input.addEventListener("input", () => {
      verifyButton.disabled =
        !input.value.trim() && !credential.configured && credential.writable === false;
      if (!error.hidden) error.hidden = true;
    });

    actions.append(saveButton, verifyButton);

    if (credential.configured && credential.writable !== false) {
      const clearButton = document.createElement("button");
      clearButton.type = "button";
      clearButton.className = "button button--danger";
      clearButton.textContent = "Remove";
      clearButton.addEventListener("click", () =>
        this.clearCredential(provider, error),
      );
      actions.append(clearButton);
    }

    const meta = document.createElement("span");
    meta.className = "provider__meta";
    meta.textContent = `Get one at ${provider.docs_url}`;

    body.append(label, input, error, actions, meta);

    if (credential.configured && credential.writable === false) {
      // A field you cannot edit, with no explanation, is the worst possible
      // outcome: it looks like a bug in the app rather than a precedence rule.
      // Name the cause and the fix.
      const locked = document.createElement("p");
      locked.className = "notice";
      locked.style.marginTop = "8px";
      locked.textContent =
        "This key comes from an environment variable or a .env file, which takes " +
        "precedence over anything saved here — so editing it in the app would appear " +
        "to do nothing. Unset it (or remove it from .env) and restart to manage it here.";
      body.append(locked);
    }

    card.append(body);
    return card;
  }

  async saveCredential(provider, input, error, button) {
    const value = input.value.trim();
    error.hidden = true;
    if (!value) {
      error.textContent = "Enter a key first.";
      error.hidden = false;
      return;
    }

    button.disabled = true;
    try {
      const response = await fetch(`/api/credentials/${provider.api_key_env}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
      const payload = await response.json();

      if (!response.ok) {
        error.textContent = payload.error || "That key was rejected.";
        error.hidden = false;
        input.setAttribute("aria-invalid", "true");
        return;
      }

      // Never keep the typed secret in the DOM once it has been stored.
      input.value = "";
      input.removeAttribute("aria-invalid");
      await this.reload();
      if (this.onSaved) this.onSaved(this.described);
      if (this.onToast) this.onToast(`${provider.label} key saved.`, "ok");
    } catch (cause) {
      error.textContent = `Could not save: ${cause.message}`;
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  }

  async verifyCredential(provider, input, button, error) {
    error.hidden = true;
    const original = button.textContent;
    button.textContent = "Testing…";
    button.disabled = true;
    const draft = input.value.trim();
    try {
      const response = await fetch(`/api/credentials/${provider.api_key_env}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // The draft, when present, is tested without being saved.
        body: JSON.stringify(draft ? { draft } : {}),
      });
      const payload = await response.json();
      if (!response.ok) {
        error.textContent = payload.error || "That key did not work.";
        error.hidden = false;
      } else if (this.onToast) {
        this.onToast(`${provider.label} key works.`, "ok");
      }
    } catch (cause) {
      error.textContent = `Could not test: ${cause.message}`;
      error.hidden = false;
    } finally {
      button.textContent = original;
      button.disabled = false;
    }
  }

  async clearCredential(provider, error) {
    error.hidden = true;
    const response = await fetch(`/api/credentials/${provider.api_key_env}`, {
      method: "DELETE",
    });
    const payload = await response.json();
    if (!response.ok) {
      error.textContent = payload.error || "Could not remove that key.";
      error.hidden = false;
      return;
    }
    await this.reload();
    if (this.onSaved) this.onSaved(this.described);
    if (this.onToast) this.onToast(`${provider.label} key removed.`, "ok");
  }
}

function keyValue(key, value) {
  const row = document.createElement("div");
  const label = document.createElement("span");
  label.className = "detail__key";
  label.textContent = key;
  const content = document.createElement("span");
  content.className = "detail__value";
  content.textContent = value || "—";
  row.append(label, content);
  return row;
}
