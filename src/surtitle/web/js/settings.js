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

export class SettingsPanel {
  constructor({ onSaved, onToast, onMicrophoneChange, onSpeakerChange, playback }) {
    this.onSaved = onSaved;
    this.onToast = onToast;
    this.onMicrophoneChange = onMicrophoneChange;
    this.onSpeakerChange = onSpeakerChange;
    // Needed to ask whether this browser supports output selection at all.
    this.playback = playback;
    this.described = null;
    this.activeSection = "model";
    // Set while a device picker is on screen, so a device that appears or is
    // unplugged updates it. Without this the list is whatever it was when the panel
    // was opened: granting permission by using the microphone left the page saying
    // "one input, unnamed" for the rest of the session, because nothing asked again.
    this.refreshDevices = null;
    if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
      navigator.mediaDevices.addEventListener("devicechange", () => {
        if (this.refreshDevices) this.refreshDevices();
      });
    }
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

  /**
   * The navigation is the server's capability table, not a list kept here.
   *
   * There is no separate "API keys" page: every provider belongs to the capability
   * it serves, and its key is configured in that section. A page of keys apart from
   * the choice that needs them is how a key gets saved without anything using it,
   * and how somebody goes looking for "Tavily setup" and finds nothing.
   */
  renderNav() {
    const order = this.described.section_order || Object.keys(this.described.sections || {});
    const labels = this.described.section_labels || {};
    const present = new Set(Object.keys(this.described.sections || {}));
    const ordered = order.filter((name) => present.has(name));
    for (const name of present) {
      if (!ordered.includes(name)) ordered.push(name);
    }

    this.nav.replaceChildren();
    for (const section of [...ordered, "microphone"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "modal__navItem";
      button.textContent = section === "microphone" ? "Microphone" : labels[section] || section;
      button.setAttribute("aria-current", String(this.activeSection === section));
      button.addEventListener("click", () => {
        this.activeSection = section;
        this.renderNav();
        this.render();
      });
      this.nav.append(button);
    }
  }

  /** The capability a section is about, or null for a section that is not one. */
  capabilityFor(section) {
    return (this.described.capabilities || []).find((item) => item.id === section) || null;
  }

  render() {
    this.refreshDevices = null;
    this.micPicker = null;
    this.speakerPicker = null;
    const labels = this.described.section_labels || {};
    this.title.textContent =
      this.activeSection === "microphone"
        ? "Microphone"
        : labels[this.activeSection] || this.activeSection;
    this.body.replaceChildren();

    if (this.activeSection === "microphone") {
      this.renderMicrophone();
      return;
    }

    const fields = this.described.sections[this.activeSection] || [];
    this.renderFields(this.activeSection, fields);
    // The provider a capability is served by, right under the choice between them:
    // its key, whether it is ready, and how to install it if it runs here.
    const capability = this.capabilityFor(this.activeSection);
    if (capability) this.renderProviders(capability);
    if (this.activeSection === "general") this.renderStorage();
  }

  /**
   * Input-device picker.
   *
   * Relying on the browser's default input is wrong on any machine with virtual
   * audio devices — Teams, Zoom, VB-Cable, loopback drivers — where the default
   * can be a device that never carries the microphone. This lets the user name
   * the device instead of hoping.
   *
   * Device *labels* are only exposed after microphone permission is granted, so
   * the list may be anonymous until the mic has been opened once; the picker says
   * so rather than showing an unexplained empty list.
   */
  renderMicrophone() {
    const row = document.createElement("div");
    row.className = "setting";

    const text = document.createElement("div");
    text.className = "setting__text";
    const label = document.createElement("label");
    label.className = "setting__title";
    label.textContent = "Input device";
    label.htmlFor = "micDevice";
    const desc = document.createElement("span");
    desc.className = "setting__desc";
    desc.textContent =
      "Which microphone the agent listens to. Devices are listed by name once the " +
      "microphone has been opened at least once.";
    text.append(label, desc);

    const control = document.createElement("div");
    control.className = "setting__control";

    const select = document.createElement("select");
    select.className = "select";
    select.id = "micDevice";
    control.append(select);

    row.append(text, control);
    this.body.append(row);

    const status = document.createElement("p");
    status.className = "notice";
    this.body.append(status);

    const actions = document.createElement("div");
    actions.className = "provider__actions";
    actions.style.marginTop = "10px";

    const allow = document.createElement("button");
    allow.type = "button";
    allow.className = "button button--primary";
    allow.textContent = "Allow microphone access";
    allow.hidden = true;
    allow.addEventListener("click", async () => {
      allow.disabled = true;
      const granted = await this.requestMicrophoneAccess(status);
      allow.disabled = false;
      // Both lists, once more with the stream closed: the browsers that only name
      // devices while capturing have answered by now, and this is what updates the
      // "in use"/count wording either way.
      if (granted) this.refreshDevicePickers();
    });

    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "button button--ghost";
    refresh.textContent = "Refresh device list";
    refresh.addEventListener("click", () => this.populateMicrophones(select, status, refresh, allow));

    actions.append(allow, refresh);
    this.body.append(actions);

    this.micPicker = { select, status, refresh, allow };
    this.populateMicrophones(select, status, refresh, allow);
    this.refreshDevices = () => this.refreshDevicePickers();

    // `append()` returns undefined, so it cannot be styled on the way in. Written
    // the other way this threw here — which meant the output-device picker below it
    // never rendered at all, and a feature that works looked like one that does not
    // exist. There is no visible symptom except a section that stops early.
    const divider = document.createElement("hr");
    divider.style.cssText =
      "border:none;border-top:0.5px solid var(--dsw-alias-border-l2);margin:18px 0";
    this.body.append(divider);

    this.renderSpeaker();
  }

  /**
   * Output-device picker.
   *
   * Web Audio cannot choose an output device, so spoken audio is routed through
   * an <audio> element and this uses setSinkId(). Support is genuinely partial —
   * Firefox does not implement it — so an unsupported browser gets a clear
   * explanation instead of a control that silently does nothing.
   */
  renderSpeaker() {
    const supported = this.playback ? this.playback.canSelectOutput : false;

    const row = document.createElement("div");
    row.className = "setting";

    const text = document.createElement("div");
    text.className = "setting__text";
    const label = document.createElement("label");
    label.className = "setting__title";
    label.textContent = "Output device";
    label.htmlFor = "speakerDevice";
    const desc = document.createElement("span");
    desc.className = "setting__desc";
    desc.textContent = supported
      ? "Where the agent's voice is played."
      : "This browser cannot choose an output device, so the system default is used. Chrome, Edge and other Chromium browsers support this.";
    text.append(label, desc);

    const control = document.createElement("div");
    control.className = "setting__control";
    const select = document.createElement("select");
    select.className = "select";
    select.id = "speakerDevice";
    select.disabled = !supported;
    control.append(select);

    row.append(text, control);
    this.body.append(row);

    const status = document.createElement("p");
    status.className = "notice";
    this.body.append(status);

    if (!supported) {
      select.append(new Option("System default", ""));
      status.textContent = "Output selection is unavailable in this browser.";
      return;
    }

    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "button button--ghost";
    refresh.textContent = "Refresh device list";
    refresh.style.marginTop = "10px";
    refresh.addEventListener("click", () => this.populateSpeakers(select, status, refresh));
    this.body.append(refresh);

    this.speakerPicker = { select, status, refresh };
    this.populateSpeakers(select, status, refresh);
  }

  async populateSpeakers(select, status, refresh) {
    select.replaceChildren();
    status.textContent = "Checking available outputs…";
    if (refresh) refresh.disabled = true;

    const current = this.getSpeakerPreference();
    const report = await this.describeDevices("audiooutput");
    const devices = report.devices;

    select.append(new Option("System default", ""));

    for (const device of devices) {
      const option = new Option(device.label, device.deviceId);
      option.selected = Boolean(current) && device.deviceId === current;
      select.append(option);
    }

    if (current && !devices.some((device) => device.deviceId === current)) {
      const missing = new Option("Previously chosen output (not connected)", current);
      missing.selected = true;
      select.append(missing);
      status.textContent = "The saved output is not connected; the system default is in use.";
    } else {
      status.textContent = this.describeDeviceStatus(report, "output");
    }

    if (refresh) refresh.disabled = false;

    select.addEventListener("change", async () => {
      this.setSpeakerPreference(select.value);
      // Report what actually happened rather than assuming success: setSinkId can
      // reject when the page lacks permission.
      const result = this.onSpeakerChange ? await this.onSpeakerChange(select.value) : { ok: true };
      if (this.onToast) {
        if (result && result.ok) {
          const chosen = devices.find((device) => device.deviceId === select.value);
          this.onToast(chosen ? `Output set to ${chosen.label}.` : "Using the system default.", "ok");
        } else if (result && result.reason === "NotAllowedError") {
          this.onToast("Open the microphone once to allow selecting an output device.", "error");
        } else {
          this.onToast("That output device could not be selected.", "error");
        }
      }
    });
  }

  getSpeakerPreference() {
    try {
      return localStorage.getItem("surtitle.speaker") || "";
    } catch {
      return "";
    }
  }

  setSpeakerPreference(deviceId) {
    try {
      if (deviceId) localStorage.setItem("surtitle.speaker", deviceId);
      else localStorage.removeItem("surtitle.speaker");
    } catch {
      /* storage unavailable; the choice simply will not persist */
    }
  }

  async populateMicrophones(select, status, refresh, allow) {
    select.replaceChildren();
    status.textContent = "Checking available inputs…";
    if (refresh) refresh.disabled = true;

    const current = this.getMicrophonePreference();
    const report = await this.describeDevices("audioinput");
    const devices = report.devices;

    const auto = document.createElement("option");
    auto.value = "";
    auto.textContent = "System default";
    auto.selected = !current;
    select.append(auto);

    for (const device of devices) {
      const option = document.createElement("option");
      option.value = device.deviceId;
      option.textContent = device.label;
      option.selected = Boolean(current) && device.deviceId === current;
      select.append(option);
    }

    // A saved device may have been unplugged since it was chosen.
    if (current && !devices.some((device) => device.deviceId === current)) {
      const missing = document.createElement("option");
      missing.value = current;
      missing.textContent = "Previously chosen device (not currently connected)";
      missing.selected = true;
      select.append(missing);
      status.textContent =
        "The saved device is not connected. The system default will be used until it returns.";
    } else {
      status.textContent = this.describeDeviceStatus(report, "input");
    }
    // Offered only when it would change something: an unnamed or empty list is what
    // a page without the microphone sees.
    if (allow) allow.hidden = !["unnamed", "none"].includes(report.reason);

    if (refresh) refresh.disabled = false;

    select.addEventListener("change", () => {
      this.setMicrophonePreference(select.value);
      const chosen = devices.find((device) => device.deviceId === select.value);
      if (this.onMicrophoneChange) this.onMicrophoneChange(select.value);
      if (this.onToast) {
        this.onToast(chosen ? `Microphone set to ${chosen.label}.` : "Using the system default.", "ok");
      }
    });
  }

  /**
   * What the browser will say about audio devices, and why it said it.
   *
   * The count alone is not enough to explain anything. "One input" is what a page
   * with no permission sees, and it is also what a machine with one microphone
   * sees, and what a page that cannot enumerate devices at all ends up with — three
   * different situations that used to produce one message telling the user to open
   * the microphone and press refresh, which is what they had already done.
   */
  async describeDevices(kind) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      return { devices: [], reason: "unsupported", permission: null };
    }
    let found;
    try {
      found = await navigator.mediaDevices.enumerateDevices();
    } catch {
      return { devices: [], reason: "refused", permission: null };
    }
    const noun = kind === "audioinput" ? "Input" : "Output";
    const raw = found
      .filter((device) => device.kind === kind)
      .map((device, index) => ({
        deviceId: device.deviceId,
        // Whether the browser named it, decided before the placeholder goes on: a
        // list of "Input 1", "Input 2" is exactly what a page without permission
        // sees, and reporting that as "2 devices found" hides the reason.
        named: Boolean(device.label),
        label: device.label || `${noun} ${index + 1}`,
      }));
    const named = raw.length > 0 && raw.every((device) => device.named);
    const reason = raw.length === 0 ? "none" : named ? "named" : "unnamed";
    return {
      devices: raw.map(({ deviceId, label }) => ({ deviceId, label })),
      reason,
      permission: await this.microphonePermission(),
    };
  }

  /** Re-read both device lists, for whatever is on screen. */
  refreshDevicePickers() {
    const mic = this.micPicker;
    if (mic) this.populateMicrophones(mic.select, mic.status, mic.refresh, mic.allow);
    const speaker = this.speakerPicker;
    if (speaker) this.populateSpeakers(speaker.select, speaker.status, speaker.refresh);
  }

  /**
   * Ask for the microphone, from this page.
   *
   * Device names are hidden until the page asking has been granted the microphone,
   * and the page that needs them is the one being looked at. Telling somebody to go
   * and use the microphone somewhere else leaves them exactly where they started
   * when the window they are looking at is not the window that has permission —
   * which is easy to end up with, since a browser grants it per origin and a second
   * window on the other spelling of localhost is a different origin.
   *
   * The stream is stopped immediately: this is asking for permission, not recording,
   * and the application opens its own when the microphone is switched on.
   */
  async requestMicrophoneAccess(status) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      status.textContent = "This browser cannot open a microphone from this page.";
      return false;
    }
    status.textContent = "Waiting for the browser's permission prompt…";
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      // Read the list while the stream is still open. A browser is only obliged to
      // name its devices once something is capturing, and closing the stream first
      // asks the question a moment too late.
      this.refreshDevicePickers();
      for (const track of stream.getTracks()) track.stop();
      return true;
    } catch (error) {
      const name = error && error.name ? error.name : "unknown error";
      status.textContent =
        name === "NotAllowedError"
          ? "The browser refused microphone access for this page. Allow it in the " +
            "browser's site settings, then reload."
          : `Could not open the microphone: ${name}.`;
      return false;
    }
  }

  /** "granted" | "denied" | "prompt", or null where the browser will not say. */
  async microphonePermission() {
    try {
      const status = await navigator.permissions.query({ name: "microphone" });
      return status.state;
    } catch {
      return null;
    }
  }

  /** A sentence about audio devices that is true whatever the browser reported. */
  describeDeviceStatus(report, noun) {
    const count = report.devices.length;
    switch (report.reason) {
      case "unsupported":
        return (
          "This page cannot list audio devices: the browser only allows it in a " +
          "secure context, which means localhost or https."
        );
      case "refused":
        return `The browser refused to list ${noun} devices.`;
      case "none":
        return (
          report.permission === "denied"
            ? `No ${noun} devices: the microphone is blocked for this page. Allow it in ` +
              "the browser's site settings, then reload."
            : `No ${noun} devices are visible yet. Open the microphone once — the list ` +
              "updates by itself when the browser starts reporting them."
        );
      case "unnamed":
        return (
          `${count} ${noun} device(s), but the browser is hiding their names until this ` +
          "page is granted the microphone. Allow it below, or switch the microphone on — " +
          "the list updates by itself either way."
        );
      default:
        return `${count} ${noun} device(s) found.`;
    }
  }

  getMicrophonePreference() {
    try {
      return localStorage.getItem("surtitle.microphone") || "";
    } catch {
      return "";
    }
  }

  setMicrophonePreference(deviceId) {
    try {
      if (deviceId) localStorage.setItem("surtitle.microphone", deviceId);
      else localStorage.removeItem("surtitle.microphone");
    } catch {
      /* storage unavailable; the choice simply will not persist */
    }
  }

  /**
   * The fields of a section, minus the ones belonging to a provider that is not
   * selected.
   *
   * A model name means nothing to the other providers, so showing all of them puts
   * three model boxes in the Model section and leaves the reader to work out which
   * one counts. Temperature and the section's own choices belong to no provider and
   * are always shown.
   */
  visibleFields(section, fields) {
    const capability = this.capabilityFor(section);
    const chosen = capability && capability.setting ? this.valueOf(capability.setting) : null;
    return fields.filter((field) => !field.provider || field.provider === chosen);
  }

  renderFields(section, fields) {
    for (const field of this.visibleFields(section, fields)) {
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

  renderProviders(capability) {
    for (const provider of capability.providers || []) {
      this.body.append(this.buildProviderCard(provider, capability));
    }
  }

  /** Where the files live and what happens to a key. Storage, not configuration. */
  renderStorage() {
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

  /**
   * One provider, configured where it is used.
   *
   * Three shapes, because they need three different things from the user: an API
   * key for somebody else's service, an install for an engine that runs here, and
   * nothing at all for a provider this application performs itself. A card that
   * always shows a key field is how the keyless one came to have a blank box in it.
   */
  buildProviderCard(provider, capability) {
    const credential = provider.credential || {};
    const selected =
      capability && capability.setting
        ? (capability.providers.find((item) => item.id === provider.id) && this.valueOf(capability.setting)) ===
          provider.id
        : false;

    const card = document.createElement("div");
    card.className = "provider";
    if (selected) card.dataset.selected = "true";

    const head = document.createElement("div");
    head.className = "provider__head";

    const dot = document.createElement("span");
    dot.className = `provider__dot ${this.providerIsReady(provider) ? "provider__dot--on" : "provider__dot--off"}`;
    dot.setAttribute("role", "img");
    dot.setAttribute("aria-label", this.providerIsReady(provider) ? "Ready" : "Not ready");

    const name = document.createElement("span");
    name.className = "provider__name";
    name.textContent = provider.label;

    head.append(dot, name);

    if (selected) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "in use";
      head.append(badge);
    }
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

    if (!provider.api_key_env) {
      // Nothing to type. Say what it is and, if it has to be installed, whether it
      // is here yet — with the action beside the fact.
      body.append(this.buildKeylessBody(provider));
      card.append(body);
      return card;
    }

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

  /** The value a preference currently holds, from the payload we were given. */
  valueOf(name) {
    for (const fields of Object.values(this.described.sections || {})) {
      const field = fields.find((item) => item.name === name);
      if (field) return field.value;
    }
    return null;
  }

  /**
   * Whether a provider can serve right now.
   *
   * An API provider needs its key; a local one needs to be installed; one that this
   * application performs itself always can. The dot in the corner of the card is
   * the same question for all three.
   */
  providerIsReady(provider) {
    if (provider.kind === "local") return Boolean(provider.local && provider.local.ready);
    if (provider.kind === "builtin") return true;
    return Boolean(provider.credential && provider.credential.configured);
  }

  /** What a provider that needs no key needs instead: nothing, or an install. */
  buildKeylessBody(provider) {
    const wrap = document.createElement("div");

    const note = document.createElement("p");
    note.className = "notice";
    note.style.marginTop = "0";
    note.textContent =
      provider.kind === "local"
        ? "Recognises and speaks on this machine. Nothing leaves it, and nothing is billed."
        : "This application does the search itself, so there is no key and no account. " +
          "It is slower and rate-limited compared with a search API.";
    wrap.append(note);

    if (provider.kind === "local" && provider.local) {
      const status = document.createElement("p");
      status.className = "provider__meta";
      status.textContent = provider.local.ready
        ? `Ready. ${provider.local.detail}.`
        : provider.local.detail.charAt(0).toUpperCase() + provider.local.detail.slice(1) + "." +
          (provider.local.missing_bytes
            ? ` About ${Math.round(provider.local.missing_bytes / (1024 * 1024))} MB to download.`
            : "");
      wrap.append(status);

      if (!provider.local.ready) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button button--primary";
        button.textContent = "Install";
        button.addEventListener("click", () => this.startVoiceInstall(button, status));
        wrap.append(button);
      }
    }

    // A provider with an endpoint can be asked whether it is there. That is the
    // whole question for one running on this machine, and there is no key field
    // here to hang the Test button on.
    if (provider.base_url && provider.kind === "local") {
      const actions = document.createElement("div");
      actions.className = "provider__actions";
      const test = document.createElement("button");
      test.type = "button";
      test.className = "button button--ghost";
      test.textContent = "Test connection";
      test.addEventListener("click", () => this.testProvider(provider, test, wrap));
      actions.append(test);
      wrap.append(actions);
    }

    if (provider.docs_url) {
      const meta = document.createElement("p");
      meta.className = "provider__meta";
      meta.textContent = "More at " + provider.docs_url;
      wrap.append(meta);
    }

    return wrap;
  }

  /**
   * Ask the server whether a provider is reachable, and say what it found.
   *
   * The server makes the request because the browser cannot: a local model server
   * does not send CORS headers, so a fetch from the page would fail for a server
   * that is running perfectly.
   */
  async testProvider(provider, button, wrap) {
    button.disabled = true;
    const was = button.textContent;
    button.textContent = "Testing…";
    let result = null;
    try {
      const response = await fetch(`/api/providers/${provider.id}/verify`, { method: "POST" });
      result = await response.json();
      if (!response.ok) {
        if (this.onToast) this.onToast(result.error || "That provider could not be reached.", "error");
        return;
      }
      const models = result.models || [];
      const note = document.createElement("p");
      note.className = "provider__meta";
      note.textContent = models.length
        ? `Answered at ${result.url}. It offers: ${models.slice(0, 8).join(", ")}` +
          (models.length > 8 ? ` and ${models.length - 8} more.` : ".")
        : `Answered at ${result.url}, but listed no models.`;
      wrap.append(note);
      if (this.onToast) this.onToast(`${provider.label} is answering.`, "ok");
    } catch (cause) {
      if (this.onToast) this.onToast(`Could not test: ${cause.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = was;
    }
  }

  /**
   * Start the local-engine download and report what happens.
   *
   * The server owns the job and reports progress on the tray poll, so this asks for
   * it to start and then says so; the Settings panel is not the place to render a
   * progress bar for something that outlives it.
   */
  async startVoiceInstall(button, status) {
    button.disabled = true;
    button.textContent = "Starting…";
    try {
      // No body: the endpoint installs everything it is configured for, and a body
      // saying so would be a second place for the two to disagree.
      const response = await fetch("/api/voice/install", { method: "POST" });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 409) {
        // A state rather than a fault: one is already downloading.
        status.textContent =
          "An install is already running. Progress is shown in the tray.";
        button.textContent = "Installing…";
        return;
      }
      if (!response.ok) {
        status.textContent = payload.error || "Could not start the install.";
        button.disabled = false;
        button.textContent = "Install";
        return;
      }
      button.textContent = "Installing…";
      status.textContent =
        "Downloading the engines and their models. This continues in the background; " +
        "the tray shows progress.";
      if (this.onToast) this.onToast("Downloading the local speech engines.", "ok");
    } catch (cause) {
      status.textContent = `Could not start the install: ${cause.message}`;
      button.disabled = false;
      button.textContent = "Install";
    }
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
