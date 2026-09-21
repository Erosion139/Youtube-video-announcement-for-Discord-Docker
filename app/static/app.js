"use strict";

const state = {
  status: null,
  settings: null,
  channels: [],
  guilds: [],
  filter: "",
};
const rows = new Map();      // channel id -> row controller
const pickers = new Set();   // live Discord channel pickers
let defaultPicker = null;

/* ---- small helpers ---------------------------------------------------- */

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.filter(c => c !== null && c !== undefined));
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const options = { method, headers: { "X-Requested-With": "fetch" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch { /* empty body */ }
  if (!resp.ok) throw new Error((data && data.error) || `The server answered ${resp.status}.`);
  return data;
}

function toast(message, isError = false) {
  const node = el("div", { class: "toast" + (isError ? " is-error" : ""), text: message });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), isError ? 7000 : 3500);
}

async function withBusy(button, fn) {
  if (button) { button.classList.add("is-busy"); button.disabled = true; }
  try { return await fn(); }
  catch (err) { toast(err.message, true); return undefined; }
  finally { if (button) { button.classList.remove("is-busy"); button.disabled = false; } }
}

function relTime(ts) {
  if (!ts) return "never";
  const diff = Date.now() / 1000 - ts;
  if (diff < 0) {
    const ahead = -diff;
    if (ahead < 60) return "in under a minute";
    if (ahead < 3600) return `in ${Math.round(ahead / 60)} min`;
    return `in ${Math.round(ahead / 3600)} h`;
  }
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  if (diff < 86400 * 7) {
    const days = Math.round(diff / 86400);
    return `${days} day${days === 1 ? "" : "s"} ago`;
  }
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/* Same rules as the server's build_message(). */
function buildMessage(template, channel, title, url) {
  template = (template || "").trim();
  if (!template) return url;
  const values = { channel: channel || "", title: title || "", url };
  const text = template.replace(/\{(channel|title|url)\}/g, (_, key) => values[key]);
  return template.includes("{url}") ? text : `${text}\n${url}`;
}

function discordChannelName(id) {
  for (const guild of state.guilds) {
    const match = guild.channels.find(c => c.id === id);
    if (match) return `#${match.name}`;
  }
  return "";
}

/* ---- Discord channel picker ------------------------------------------ */

function makePicker({ value = "", allowDefault = false, id } = {}) {
  const select = el("select", { id });
  const input = el("input", { type: "text", inputmode: "numeric", placeholder: "Discord channel ID", spellcheck: "false" });
  input.hidden = true;
  const wrap = el("div", { class: "picker" }, select, input);
  const picker = { el: wrap, select, input, allowDefault, current: value, built: false, onchange: null };

  picker.getValue = () => (select.value === "__custom" ? input.value.trim() : select.value);

  picker.refresh = (nextValue) => {
    const keep = nextValue !== undefined ? nextValue : (picker.built ? picker.getValue() : picker.current);
    const typingCustom = picker.built && select.value === "__custom" && nextValue === undefined;
    select.replaceChildren();
    if (allowDefault) {
      const name = discordChannelName(state.settings?.discord_channel_id || "");
      select.append(el("option", { value: "", text: name ? `Default channel (${name})` : "Default channel" }));
    } else {
      select.append(el("option", { value: "", text: "Choose a channel" }));
    }
    const known = new Set();
    for (const guild of state.guilds) {
      const group = el("optgroup", { label: guild.name });
      for (const ch of guild.channels) {
        known.add(ch.id);
        group.append(el("option", { value: ch.id, text: `#${ch.name}${ch.can_send ? "" : " (bot can't post here)"}` }));
      }
      if (guild.channels.length) select.append(group);
    }
    if (keep && !known.has(keep) && !typingCustom) {
      select.append(el("option", { value: keep, text: `Channel ID ${keep}` }));
    }
    select.append(el("option", { value: "__custom", text: "Enter a channel ID…" }));
    if (typingCustom) {
      select.value = "__custom";
    } else {
      select.value = keep || "";
      input.hidden = true;
      input.value = "";
    }
    picker.built = true;
  };

  select.addEventListener("change", () => {
    input.hidden = select.value !== "__custom";
    if (!input.hidden) input.focus();
    picker.onchange?.();
  });
  input.addEventListener("input", () => picker.onchange?.());

  pickers.add(picker);
  picker.refresh();
  return picker;
}

function refreshPickers() {
  for (const picker of pickers) picker.refresh();
}

async function loadDiscordChannels() {
  try {
    const data = await api("/api/discord/channels");
    state.guilds = data.guilds || [];
  } catch {
    state.guilds = [];
  }
  refreshPickers();
}

/* ---- status & settings ----------------------------------------------- */

function setPill(node, cls, text, title = "") {
  node.className = "pill " + (cls || "");
  node.querySelector(".pill-text").textContent = text;
  node.title = title;
}

function renderStatus() {
  const s = state.status;
  if (!s) return;
  const bot = s.bot;

  const botState = {
    connected: ["is-ok", "Bot online"],
    connecting: ["is-warn", "Bot connecting"],
    reconnecting: ["is-warn", "Bot reconnecting"],
    error: ["is-bad", "Bot can't connect"],
    not_configured: ["", "Bot not set up"],
  }[bot.status] || ["", bot.status];
  setPill($("#pill-bot"), botState[0], botState[1], bot.error);

  const poll = s.poll;
  if (poll.running) {
    setPill($("#pill-check"), "is-warn", "Checking now");
  } else if (!poll.last) {
    setPill($("#pill-check"), "", "First check soon");
  } else {
    const push = s.websub.enabled && s.websub.active > 0;
    setPill($("#pill-check"), push ? "is-push" : "is-ok",
      `${push ? "Instant, checked" : "Checked"} ${relTime(poll.last)}`,
      poll.next ? `Next check ${relTime(poll.next)}` : "");
  }

  const line = $("#bot-line");
  const lineText = {
    connected: `Online as ${bot.user} in ${bot.guild_count} server${bot.guild_count === 1 ? "" : "s"}`,
    connecting: "Connecting to Discord…",
    reconnecting: "Reconnecting to Discord…",
    error: bot.error || "Can't connect to Discord.",
    not_configured: "Not set up yet. Paste a bot token below.",
  }[bot.status] || bot.status;
  line.className = "bot-line " + botState[0];
  $("#bot-line-text").textContent = lineText;

  const invite = $("#invite-link");
  invite.hidden = !bot.invite_url;
  if (bot.invite_url) invite.href = bot.invite_url;

  // Setup banner: the single next step, if any.
  const banner = $("#setup-banner");
  let title = "", text = "", button = "";
  if (bot.status === "not_configured") {
    title = "Connect a Discord bot to start posting";
    text = "Paste your bot token in the Discord bot panel. Channels you add are watched either way, and posting starts once the bot is online.";
    button = "Set up the bot";
  } else if (bot.status === "error") {
    title = "The Discord bot can't connect";
    text = bot.error;
    button = "Check the token";
  } else if (bot.status === "connected" && bot.guild_count === 0) {
    title = "Add the bot to your Discord server";
    text = "The bot is online but isn't in any server yet. Use “Add the bot to a server” in the Discord bot panel.";
    button = "Show me";
  } else if (bot.status === "connected" && !s.default_channel_set) {
    title = "Choose where to post new videos";
    text = "Pick a Discord channel in the Discord bot panel. Each YouTube channel can also post somewhere else.";
    button = "Choose channel";
  }
  banner.hidden = !title;
  if (title) {
    $("#setup-title").textContent = title;
    $("#setup-text").textContent = text;
    $("#setup-go").textContent = button;
  }

  const counts = s.channels;
  $("#channel-count").textContent = counts.total
    ? `${counts.total} channel${counts.total === 1 ? "" : "s"}, ${counts.enabled} on`
    : "";

  const ws = $("#websub-status");
  if (s.websub.enabled) {
    let msg = `Active for ${s.websub.active} of ${counts.enabled} channel${counts.enabled === 1 ? "" : "s"}.`;
    if (s.websub.active < counts.enabled) {
      msg += " The rest are waiting for YouTube to confirm. If that doesn't change within a few minutes, YouTube can't reach the address above.";
    }
    ws.textContent = msg;
  } else {
    ws.textContent = "Off. New uploads are found by the regular checks.";
  }

  for (const row of rows.values()) row.updatePreview();
}

function renderSettings() {
  const s = state.settings;
  if (!s) return;
  $("#token-input").placeholder = s.discord_token_set ? `Saved, ${s.discord_token_hint}` : "Paste your bot token";
  $("#token-hint").textContent = s.discord_token_set
    ? "Paste a new token to replace the saved one. Tokens are never shown again after saving."
    : "The token is stored in this app's data folder and never shown again.";
  if (!defaultPicker) {
    defaultPicker = makePicker({ value: s.discord_channel_id, id: "default-channel" });
    $("#default-channel-slot").append(defaultPicker.el);
  } else {
    defaultPicker.refresh(s.discord_channel_id);
  }
  $("#default-message").value = s.default_message;
  $("#poll-minutes").value = Math.round(s.poll_interval / 60);
  $("#max-age").value = s.max_age_hours;
  $("#public-url").value = s.public_url;
  if (s.public_url) $("#websub-details").open = true;
  $("#apikey-hint").textContent = s.youtube_api_key_set ? `A key is saved (${s.youtube_api_key_hint}).` : "No key saved.";
  $("#apikey-clear").hidden = !s.youtube_api_key_set;
  refreshPickers();
}

async function refreshStatus() {
  const previous = state.status?.bot?.status;
  state.status = await api("/api/status");
  const now = state.status.bot.status;
  if (now === "connected" && (previous !== "connected" || !state.guilds.length)) await loadDiscordChannels();
  if (now !== "connected" && previous === "connected") { state.guilds = []; refreshPickers(); }
  renderStatus();
}

async function saveSettings(body, button, success) {
  const result = await withBusy(button, () => api("/api/settings", { method: "PUT", body }));
  if (result) {
    state.settings = result;
    renderSettings();
    if (success) toast(success);
    refreshStatus().catch(() => {});
  }
  return result;
}

/* ---- channel rows ----------------------------------------------------- */

function renderRichText(container, text) {
  container.replaceChildren();
  const pattern = /(https?:\/\/\S+|@everyone|@here|<@&\d+>|<@!?\d+>|<#\d+>)/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) container.append(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("http")) {
      container.append(el("a", { href: token, target: "_blank", rel: "noopener", text: token }));
    } else {
      let label = token;
      if (token.startsWith("<@&")) label = "@role";
      else if (token.startsWith("<@")) label = "@user";
      else if (token.startsWith("<#")) label = "#channel";
      container.append(el("span", { class: "dc-mention", text: label }));
    }
    last = match.index + token.length;
  }
  if (last < text.length) container.append(text.slice(last));
}

function createRow(channel) {
  const li = $("#tpl-row").content.firstElementChild.cloneNode(true);
  const optionsId = `ch-options-${channel.id}`;
  const row = {
    id: channel.id, data: channel, el: li, dirty: false, pending: false, picker: null,
    toggle: $(".ch-toggle", li),
    expand: $(".ch-expand", li),
    options: $(".ch-options", li),
    message: $(".ch-message", li),
    shorts: $(".ch-shorts", li),
    save: $(".ch-save", li),
  };
  row.options.id = optionsId;
  row.expand.setAttribute("aria-controls", optionsId);
  const messageId = `ch-message-${channel.id}`;
  row.message.id = messageId;
  $(".ch-label", li).setAttribute("for", messageId);

  row.picker = makePicker({ value: channel.discord_channel_id, allowDefault: true, id: `ch-target-${channel.id}` });
  $(".ch-picker-slot", li).append(row.picker.el);
  li.querySelectorAll(".ch-label")[1].setAttribute("for", `ch-target-${channel.id}`);

  const markDirty = () => {
    row.dirty = true;
    row.save.disabled = false;
    row.updatePreview();
  };
  row.picker.onchange = markDirty;
  row.message.addEventListener("input", markDirty);
  row.shorts.addEventListener("change", markDirty);

  row.syncedKey = null;
  row.syncForm = () => {
    row.syncedKey = JSON.stringify([row.data.message, row.data.discord_channel_id, row.data.include_shorts]);
    row.message.value = row.data.message;
    row.message.placeholder = `e.g. ${row.data.name} uploaded a video!`;
    row.shorts.checked = row.data.include_shorts;
    row.picker.refresh(row.data.discord_channel_id);
    row.dirty = false;
    row.save.disabled = true;
  };

  row.update = () => {
    const ch = row.data;
    li.classList.toggle("is-off", !ch.enabled);

    const name = $(".ch-name", li);
    name.textContent = ch.name;
    name.href = ch.url;
    name.title = ch.handle ? `${ch.handle} on YouTube` : "Open on YouTube";

    const avatar = $(".ch-avatar", li);
    if (ch.thumbnail) {
      avatar.style.backgroundImage = `url("${ch.thumbnail.replace(/"/g, "%22")}")`;
      avatar.textContent = "";
    } else {
      avatar.style.backgroundImage = "";
      avatar.textContent = (ch.name || "?").trim().charAt(0).toUpperCase();
    }

    const sub = $(".ch-sub", li);
    sub.classList.toggle("is-error", Boolean(ch.last_error) && ch.enabled);
    if (ch.last_error && ch.enabled) sub.textContent = ch.last_error;
    else if (ch.last_video_title) sub.textContent = `Latest: “${ch.last_video_title}”, ${relTime(ch.last_video_published)}`;
    else if (!ch.last_checked) sub.textContent = "Waiting for the first check";
    else sub.textContent = "No public videos yet";

    const stateEl = $(".ch-state", li);
    let cls = "is-ok", text = "Watching", tip = `Checked ${relTime(ch.last_checked)}`;
    if (!ch.enabled) { cls = ""; text = "Off"; tip = "Not posting new uploads"; }
    else if (ch.last_error) { cls = "is-bad"; text = "Problem"; tip = ch.last_error; }
    else if (ch.push === "active") { cls = "is-push"; text = "Instant"; tip = `Instant notifications on. ${tip}`; }
    stateEl.className = "ch-state " + cls;
    $(".ch-state-text", stateEl).textContent = text;
    stateEl.title = tip;

    if (!row.pending) row.toggle.checked = ch.enabled;
    row.toggle.setAttribute("aria-label", `Post new uploads from ${ch.name}`);
    $(".sr-only", row.expand).textContent = `Options for ${ch.name}`;
    const key = JSON.stringify([ch.message, ch.discord_channel_id, ch.include_shorts]);
    if (!row.dirty && key !== row.syncedKey) row.syncForm();
    row.message.placeholder = `e.g. ${ch.name} uploaded a video!`;
    row.updatePreview();
  };

  row.updatePreview = () => {
    if (row.options.hidden) return;
    const ch = row.data;
    const bot = state.status?.bot || {};
    const videoId = ch.last_video_id;
    const url = videoId ? `https://www.youtube.com/watch?v=${videoId}` : "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
    const title = ch.last_video_title || "Your next video";
    const template = row.message.value.trim() || state.settings?.default_message || "";
    $(".dc-name", li).textContent = bot.user || "Your bot";
    $(".dc-avatar", li).style.backgroundImage = bot.avatar ? `url("${bot.avatar}")` : "";
    renderRichText($(".dc-text", li), buildMessage(template, ch.name, title, url));
    $(".dc-embed-author", li).textContent = ch.name;
    $(".dc-embed-title", li).textContent = title;
    $(".dc-thumb", li).style.backgroundImage = videoId ? `url("https://i.ytimg.com/vi/${videoId}/mqdefault.jpg")` : "";
  };

  row.expand.addEventListener("click", () => {
    const open = row.options.hidden;
    row.options.hidden = !open;
    row.expand.setAttribute("aria-expanded", String(open));
    li.classList.toggle("is-open", open);
    if (open) row.updatePreview();
  });

  row.toggle.addEventListener("change", async () => {
    const wanted = row.toggle.checked;
    row.pending = true;
    row.toggle.disabled = true;
    try {
      row.data = await api(`/api/channels/${row.id}`, { method: "PATCH", body: { enabled: wanted } });
      replaceChannel(row.data);
      toast(wanted ? `${row.data.name} is on` : `${row.data.name} is off`);
    } catch (err) {
      row.toggle.checked = !wanted;
      toast(err.message, true);
    } finally {
      row.pending = false;
      row.toggle.disabled = false;
      row.update();
      refreshStatus().catch(() => {});
    }
  });

  $(".ch-form", li).addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      message: row.message.value,
      discord_channel_id: row.picker.getValue(),
      include_shorts: row.shorts.checked,
    };
    const result = await withBusy(row.save, () => api(`/api/channels/${row.id}`, { method: "PATCH", body }));
    if (result) {
      row.data = result;
      replaceChannel(result);
      row.syncForm();
      row.update();
      toast(`Saved ${result.name}`);
    } else {
      row.save.disabled = !row.dirty;
    }
  });

  $(".ch-test", li).addEventListener("click", async (event) => {
    if (row.dirty) { toast("Save your changes first, then send a test post.", true); return; }
    const result = await withBusy(event.currentTarget, () => api(`/api/channels/${row.id}/test`, { method: "POST" }));
    if (result) toast(`Test post sent to ${result.channel}`);
  });

  $(".ch-check", li).addEventListener("click", async (event) => {
    if (!row.data.enabled) { toast("Turn this channel on to check it.", true); return; }
    const result = await withBusy(event.currentTarget, () => api(`/api/channels/${row.id}/check`, { method: "POST" }));
    if (!result) return;
    row.data = result.channel;
    replaceChannel(result.channel);
    row.update();
    if (result.error) toast(result.error, true);
    else if (result.posted) toast(`Posted ${result.posted} new video${result.posted === 1 ? "" : "s"}`);
    else if (result.failed) toast("Found a new video but couldn't post it. See the activity list.", true);
    else toast("No new videos");
    loadActivity();
  });

  $(".ch-remove", li).addEventListener("click", async (event) => {
    if (!confirm(`Stop watching ${row.data.name}? Its message and settings will be removed.`)) return;
    const result = await withBusy(event.currentTarget, () => api(`/api/channels/${row.id}`, { method: "DELETE" }));
    if (!result) return;
    state.channels = state.channels.filter(c => c.id !== row.id);
    toast(`Removed ${row.data.name}`);
    renderChannels();
    refreshStatus().catch(() => {});
  });

  row.update();
  return row;
}

function replaceChannel(channel) {
  const index = state.channels.findIndex(c => c.id === channel.id);
  if (index >= 0) state.channels[index] = channel;
}

function renderChannels() {
  const list = $("#ch-list");
  const filter = state.filter.trim().toLowerCase();
  const present = new Set();

  state.channels.forEach((channel, index) => {
    let row = rows.get(channel.id);
    if (!row) {
      row = createRow(channel);
      rows.set(channel.id, row);
    } else {
      row.data = channel;
      row.update();
    }
    present.add(channel.id);
    const matches = !filter
      || channel.name.toLowerCase().includes(filter)
      || (channel.handle || "").toLowerCase().includes(filter);
    row.el.hidden = !matches;
    if (list.children[index] !== row.el) list.insertBefore(row.el, list.children[index] || null);
  });

  for (const [id, row] of rows) {
    if (!present.has(id)) {
      pickers.delete(row.picker);
      row.el.remove();
      rows.delete(id);
    }
  }

  $("#empty").hidden = state.channels.length > 0;
  list.hidden = state.channels.length === 0;
  $("#filter-row").hidden = state.channels.length <= 6;
}

async function refreshChannels() {
  state.channels = await api("/api/channels");
  renderChannels();
}

/* ---- activity --------------------------------------------------------- */

async function loadActivity() {
  let items = [];
  try { items = await api("/api/activity"); } catch { return; }
  const list = $("#activity");
  list.replaceChildren();
  if (!items.length) {
    list.append(el("li", { class: "activity-empty", text: "Nothing yet." }));
    return;
  }
  for (const item of items) {
    list.append(el("li", { class: `lv-${item.level}` },
      el("span", { class: "dot" }),
      el("span", { text: item.message }),
      el("time", { datetime: new Date(item.ts * 1000).toISOString(), title: new Date(item.ts * 1000).toLocaleString(), text: relTime(item.ts) }),
    ));
  }
}

/* ---- wiring ----------------------------------------------------------- */

function focusPanel(selector, focusTarget) {
  const panel = $(selector);
  panel.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  panel.classList.remove("is-flash");
  void panel.offsetWidth;
  panel.classList.add("is-flash");
  if (focusTarget) setTimeout(() => focusTarget.focus({ preventScroll: true }), 300);
}

function wire() {
  $("#add-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#add-input");
    const error = $("#add-error");
    error.hidden = true;
    const button = $("#add-btn");
    button.classList.add("is-busy");
    button.disabled = true;
    try {
      const channel = await api("/api/channels", { method: "POST", body: { query: input.value } });
      input.value = "";
      await refreshChannels();
      toast(`Now watching ${channel.name}`);
      rows.get(channel.id)?.el.scrollIntoView({ block: "nearest" });
      refreshStatus().catch(() => {});
      loadActivity();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      button.classList.remove("is-busy");
      button.disabled = false;
    }
  });

  $("#filter-input").addEventListener("input", (event) => {
    state.filter = event.target.value;
    renderChannels();
  });

  $("#btn-check-all").addEventListener("click", async (event) => {
    const result = await withBusy(event.currentTarget, () => api("/api/check", { method: "POST" }));
    if (!result) return;
    toast(result.already_running ? "A check is already running" : "Checking all channels now");
    setTimeout(refreshAll, 2500);
    setTimeout(refreshAll, 8000);
  });

  $("#setup-go").addEventListener("click", () => {
    const bot = state.status?.bot || {};
    let target = $("#token-input");
    if (bot.status === "connected" && bot.guild_count === 0) target = $("#invite-link");
    else if (bot.status === "connected") target = $("#default-channel");
    focusPanel("#panel-discord", target);
  });

  $("#token-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#token-input");
    if (!input.value.trim()) { toast("Paste the bot token first.", true); return; }
    const saved = await saveSettings({ discord_token: input.value }, event.submitter, "Token saved. Connecting to Discord…");
    if (saved) {
      input.value = "";
      setTimeout(() => refreshStatus().catch(() => {}), 2000);
      setTimeout(() => refreshStatus().catch(() => {}), 6000);
    }
  });

  $("#default-channel-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = defaultPicker.getValue();
    const name = discordChannelName(value);
    await saveSettings({ discord_channel_id: value }, event.submitter,
      value ? `New videos will post in ${name || "channel " + value}` : "Default channel cleared");
  });

  $("#btn-test-discord").addEventListener("click", async (event) => {
    const result = await withBusy(event.currentTarget, () =>
      api("/api/discord/test", { method: "POST", body: { channel_id: defaultPicker.getValue() } }));
    if (result) toast(`Test message sent to ${result.channel}`);
  });

  $("#default-message-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await saveSettings({ default_message: $("#default-message").value }, event.submitter, "Default message saved");
  });

  $("#checking-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const minutes = parseInt($("#poll-minutes").value, 10);
    const hours = parseInt($("#max-age").value, 10);
    await saveSettings({ poll_interval: minutes * 60, max_age_hours: hours }, event.submitter, "Checking settings saved");
  });

  $("#websub-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = $("#public-url").value.trim();
    await saveSettings({ public_url: value }, event.submitter,
      value ? "Saved. Asking YouTube to send instant notifications…" : "Instant notifications turned off");
    setTimeout(refreshAll, 5000);
  });

  $("#apikey-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#apikey-input");
    if (!input.value.trim()) { toast("Paste the API key first.", true); return; }
    if (await saveSettings({ youtube_api_key: input.value }, event.submitter, "API key saved")) input.value = "";
  });

  $("#apikey-clear").addEventListener("click", async (event) => {
    await saveSettings({ clear_youtube_api_key: true }, event.currentTarget, "API key removed");
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshAll();
  });
}

let refreshing = false;
async function refreshAll() {
  if (refreshing) return;
  refreshing = true;
  try {
    await Promise.all([refreshStatus(), refreshChannels(), loadActivity()]);
  } catch (err) {
    console.warn(err);
  } finally {
    refreshing = false;
  }
}

async function start() {
  wire();
  try {
    state.settings = await api("/api/settings");
    renderSettings();
  } catch (err) {
    toast(`Couldn't load settings: ${err.message}`, true);
  }
  await refreshAll();
  setInterval(() => {
    if (document.visibilityState === "visible") refreshAll();
  }, 15000);
}

start();
