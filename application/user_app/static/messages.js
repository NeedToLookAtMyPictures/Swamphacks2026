// Messages tab: one conversation per person in a sidebar (Everyone pinned to the
// top); clicking one shows its history and sends new messages to that person.

let myName = null;
let selected = null;   // receiver of the open conversation: BROADCAST or a name

const BROADCAST = "*";        // receiver for messages to everyone
const OTHER = "__other__";    // "New chat" dropdown value that reveals the free-text box

const sameName = (a, b) => a.toLowerCase() === b.toLowerCase();

function msgEl(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;  // textContent, so messages can't inject HTML
  if (className) node.className = className;
  return node;
}

function describeContact(c) {
  if (c.latitude === null) return "heard of";
  const when = new Date(c.position_time * 1000).toLocaleString();
  return `seen ${when} near ${c.latitude.toFixed(4)}, ${c.longitude.toFixed(4)}`;
}

// Which conversation a message belongs to: Everyone, or the other person
function conversationOf(m) {
  if (m.receiver === BROADCAST) return BROADCAST;
  return sameName(m.sender, myName) ? m.receiver : m.sender;
}

const inConversation = (m, who) =>
  who === BROADCAST ? m.receiver === BROADCAST
                    : m.receiver !== BROADCAST && sameName(conversationOf(m), who);

function renderMessage(m) {
  const outgoing = sameName(m.sender, myName);
  const broadcast = m.receiver === BROADCAST;
  const card = msgEl("div", undefined, `message ${outgoing ? "outgoing" : "incoming"}`);
  // Messages to me are marked dead as soon as they arrive (delivered), so only
  // grey out my own messages once they've been received or expired
  if (m.is_dead && outgoing) card.classList.add("dead");

  const head = msgEl("div", undefined, "message-head");
  head.appendChild(msgEl("strong", outgoing ? "You" : m.sender));
  const right = msgEl("span", undefined, "message-head-right");
  // Date only; the time is in the details box (the "i" button)
  right.appendChild(msgEl("span", new Date(m.time_created * 1000).toLocaleDateString()));
  head.appendChild(right);
  card.appendChild(head);
  card.appendChild(msgEl("div", m.content, "message-body"));

  if (m.is_dead && outgoing) {
    // received: the delivered tombstone reached this device within 31 days
    const note = broadcast ? "expired" : m.received ? "received" : "received or died";
    card.appendChild(msgEl("div", note, "message-note"));
  }
  addMessageInfo(card, right, m, outgoing);  // last, so the details box sits below everything
  return card;
}

// Every message has an "i" button with the same details: when it was sent, when it was
// received and how it came (LoRa, ESP-NOW, the server). For our own messages those come
// from the receiver's delivery receipt. Which ones are open survives the 1 s redraws.
const openInfo = new Set();

function fmtTime(seconds) {
  return new Date(seconds * 1000).toLocaleString();
}

let receipts = new Map();  // "origin_id/seq" of our message -> its "delivered" tombstone

const viaLabel = (via) => (via ? VIA_LABELS[via] || via : "not recorded");

// Sent / Received / Came by for one of our own messages, from its delivery receipt.
// "Came by" is how the receipt reached us: the message's own route over the last hop
// when the two devices synced directly, which is the usual case.
function sentMessageRows(m) {
  const sent = ["Sent", fmtTime(m.time_created)];
  if (m.receiver === BROADCAST) {
    return [sent, ["Received", m.is_dead ? "expired (no receipts for Everyone)" : "by everyone it reaches"],
            ["Came by", "no receipts for Everyone"]];
  }
  const receipt = receipts.get(`${m.origin_id}/${m.seq}`);
  if (receipt) return [sent, ["Received", fmtTime(receipt.time_created)], ["Came by", viaLabel(receipt.received_via)]];
  if (m.is_dead) return [sent, ["Received", "unknown (receipt expired)"], ["Came by", "unknown"]];
  return [sent, ["Received", "not yet"], ["Came by", "–"]];
}

function addMessageInfo(card, headRight, m, outgoing) {
  const key = `${m.origin_id}/${m.seq}`;
  const button = msgEl("button", "i", "message-info-btn");
  button.type = "button";
  button.title = "Message details";
  button.setAttribute("aria-label", "Message details");
  headRight.appendChild(button);

  const info = msgEl("dl", undefined, "message-info");
  let rows;
  if (outgoing) {
    rows = sentMessageRows(m);
  } else {
    // No received_via on someone else's message: it arrived before the app recorded how
    rows = [["Sent", fmtTime(m.time_created)],
            ["Received", fmtTime(m.time_received)],
            ["Came by", viaLabel(m.received_via)]];
  }
  for (const [label, value] of rows) {
    info.append(msgEl("dt", label), msgEl("dd", value));
  }
  card.appendChild(info);

  const show = (open) => {
    info.hidden = !open;
    card.classList.toggle("info-open", open);  // widens the card (style.css)
    button.classList.toggle("open", open);
    button.setAttribute("aria-expanded", String(open));
  };
  show(openInfo.has(key));
  button.addEventListener("click", () => {
    const open = !openInfo.has(key);
    if (open) openInfo.add(key); else openInfo.delete(key);
    show(open);
  });
}

let lastMessages = [];
let lastPeople = [];
let lastSnapshot = null;  // JSON of the data last drawn, so refreshes skip unchanged redraws

async function loadMessages() {
  const list = document.getElementById("message-list");
  const deviceRes = await fetch("/api/device");
  if (!deviceRes.ok) {
    lastSnapshot = null;
    document.getElementById("chat-list").replaceChildren();
    document.getElementById("chat-title").textContent = "";
    list.replaceChildren(msgEl("p", "Set up this device first (Mesh tab).", "message-note"));
    return;
  }
  const device = await deviceRes.json();
  myName = device.name;

  const [messages, contacts, gps, tombstones] = await Promise.all([
    fetch("/api/messages").then((r) => r.json()),
    fetch("/api/contacts").then((r) => r.json()),
    fetch("/api/geolocations").then((r) => r.json()),
    fetch("/api/tombstones").then((r) => r.json()),
  ]);

  await updateQueuedStatus(device, tombstones);

  // Redrawing resets the New chat dropdown and the scroll position, so skip it
  // when nothing changed since the last refresh
  const snapshot = JSON.stringify([myName, selected, messages, contacts, gps, tombstones]);
  if (snapshot === lastSnapshot) return;
  lastSnapshot = snapshot;

  // The receiver's device makes a "delivered" tombstone the moment one of our messages
  // reaches it, so its time is when the message was received.
  receipts = new Map(tombstones.filter((t) => t.reason === "delivered")
                               .map((t) => [`${t.target_origin_id}/${t.target_seq}`, t]));

  fillNewChat(gps.filter((g) => !sameName(g.user, myName)));

  // Suggestions for the "Type a name" box: everyone this device knows about
  const options = document.getElementById("contact-list");
  options.replaceChildren(...contacts.map((c) => {
    const option = msgEl("option");
    option.value = c.name;
    option.label = describeContact(c);
    return option;
  }));

  // Only messages I sent or that are addressed to me; the rest are just being relayed
  lastMessages = messages
    .filter((m) => m.receiver === BROADCAST || [m.sender, m.receiver].some((n) => sameName(n, myName)))
    .sort((a, b) => a.time_created - b.time_created);  // oldest first, like a chat

  // One conversation per person I've messaged with
  const latest = new Map();  // lowercase name -> {name, time}
  const note = (name, time) => {
    if (!name || name === BROADCAST || sameName(name, myName)) return;
    const key = name.toLowerCase();
    const prev = latest.get(key);
    if (!prev || time > prev.time) latest.set(key, { name: prev ? prev.name : name, time });
  };
  lastMessages.forEach((m) => note(conversationOf(m), m.time_created));
  // A chat just started from the "New chat" box has no messages yet; keep it on top
  if (selected && selected !== BROADCAST && !latest.has(selected.toLowerCase())) {
    latest.set(selected.toLowerCase(), { name: selected, time: Infinity });
  }
  lastPeople = [...latest.values()]
    .sort((a, b) => b.time - a.time || a.name.localeCompare(b.name))
    .map((p) => p.name);

  if (!selected) selected = BROADCAST;
  render();
}

function render() {
  const sidebar = document.getElementById("chat-list");
  const tab = (who, label, preview) => {
    const b = msgEl("button", undefined, "chat-tab");
    if (who === BROADCAST) b.classList.add("pinned");
    if (who === selected || (who !== BROADCAST && selected !== BROADCAST && sameName(who, selected))) {
      b.classList.add("active");
    }
    b.appendChild(msgEl("strong", label));
    b.appendChild(msgEl("span", preview, "chat-preview"));
    b.addEventListener("click", () => openChat(who));
    return b;
  };
  const previewOf = (who) => {
    const last = lastMessages.filter((m) => inConversation(m, who)).at(-1);
    if (!last) return "No messages yet";
    return (sameName(last.sender, myName) ? "You: " : who === BROADCAST ? `${last.sender}: ` : "") + last.content;
  };
  sidebar.replaceChildren(
    tab(BROADCAST, "Everyone", previewOf(BROADCAST)),
    ...lastPeople.map((name) => tab(name, name, previewOf(name))),
  );

  document.getElementById("chat-title").textContent = selected === BROADCAST ? "Everyone" : selected;
  const list = document.getElementById("message-list");
  const history = lastMessages.filter((m) => inConversation(m, selected));
  list.replaceChildren(...(history.length ? history.map(renderMessage)
                                          : [msgEl("p", "No messages yet.", "message-note")]));
  list.scrollTop = list.scrollHeight;  // newest at the bottom
}

function openChat(who) {
  selected = who;
  queued = null;  // the note belongs to the conversation it was sent in
  document.getElementById("compose-status").textContent = "";
  render();
}

// "New chat" dropdown: everyone in the GPS table (newest position first), or Other
function fillNewChat(people) {
  const select = document.getElementById("new-chat-to");
  const previous = select.value;
  const option = (value, label) => {
    const o = msgEl("option", label);
    o.value = value;
    return o;
  };
  const sorted = [...people].sort((a, b) => b.time_created - a.time_created);
  const placeholder = option("", "New chat…");
  placeholder.disabled = true;
  select.replaceChildren(
    placeholder,
    ...sorted.map((g) => option(g.user,
      `${g.user} (seen ${new Date(g.time_created * 1000).toLocaleString()})`)),
    option(OTHER, "Other (type a name)…"),
  );
  // Keep the current choice across refreshes if it's still there
  select.value = [...select.options].some((o) => o.value === previous && previous) ? previous : "";
  toggleOther();
}

function toggleOther() {
  const other = document.getElementById("new-chat-name");
  other.hidden = document.getElementById("new-chat-to").value !== OTHER;
  other.required = !other.hidden;
  document.getElementById("new-chat-go").hidden = other.hidden;
}

document.getElementById("new-chat-to").addEventListener("change", () => {
  const select = document.getElementById("new-chat-to");
  if (select.value === OTHER) {
    toggleOther();
    document.getElementById("new-chat-name").focus();
  } else if (select.value) {
    startChat(select.value);  // picking someone from GPS opens their chat right away
  }
});

document.getElementById("new-chat").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("new-chat-name");
  startChat(input.value.trim());
});

function startChat(name) {
  const select = document.getElementById("new-chat-to");
  const input = document.getElementById("new-chat-name");
  select.value = "";
  input.value = "";
  toggleOther();
  if (!name || sameName(name, myName)) return;
  const existing = lastPeople.find((p) => sameName(p, name));
  if (!existing) lastPeople.unshift(name);
  openChat(existing || name);
}

// The "Queued for ..." note under the box stays until the message has left this device:
// delivered (its receipt came back), uploaded to the server, or handed to someone over
// the mesh. Then it says so briefly and clears.
let queued = null;  // {key, received, label} of the last message sent from this box

async function updateQueuedStatus(device, tombstones) {
  if (!queued) return;
  const q = queued;
  let how = null;
  if (tombstones.some((t) => t.reason === "delivered" && `${t.target_origin_id}/${t.target_seq}` === q.key)) {
    how = `Delivered to ${q.label}.`;
  } else if (device.last_push && device.last_push >= q.received) {
    how = "Sent (via the server).";
  } else {
    // A mesh exchange that finished after the message was written and sent something
    const mesh = await fetch("/api/mesh").then((r) => r.json()).catch(() => null);
    const peers = (mesh && mesh.node && mesh.node.peers) || [];
    const hop = peers.find((p) => p.last_sync && p.last_sync.ok && p.last_sync.at >= q.received && p.last_sync.sent > 0);
    if (hop) how = `Sent (passed to ${hop.name} over ${hop.last_sync.radio}).`;
  }
  if (!how || queued !== q) return;
  queued = null;
  const status = document.getElementById("compose-status");
  status.textContent = how;
  setTimeout(() => { if (status.textContent === how) status.textContent = ""; }, 3000);
}

document.getElementById("compose").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!selected) return;
  const receiver = selected;
  const label = receiver === BROADCAST ? "everyone" : receiver;
  const content = document.getElementById("compose-content");
  const status = document.getElementById("compose-status");
  const res = await fetch("/api/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ receiver, content: content.value }),
  });
  if (res.ok) {
    const m = await res.json();
    queued = { key: `${m.origin_id}/${m.seq}`, received: m.time_received, label };
    status.textContent = `Queued for ${label}. It will be passed along as you meet people.`;
    content.value = "";
    loadMessages();
  } else {
    status.textContent = (await res.json()).error;
  }
});

// The Send button's colour says how fast a message would leave right now (GET /api/connection):
// blue = fast (the server, or a nearby device over ESP-NOW), green = LoRa only (slow),
// grey = nothing in reach (it still sends: the message waits and spreads later).
const CONNECTION_REFRESH_MS = 2000;
const CONNECTION_TEXT = { fast: "fast connection", lora: "LoRa only (slower)", none: "nobody in reach yet" };

async function updateSendButton() {
  const c = await fetch("/api/connection").then((r) => r.json());
  const button = document.getElementById("compose-send");
  button.classList.remove("send-fast", "send-lora", "send-none");
  button.classList.add(`send-${c.state}`);
  const text = c.state === "none" ? `${c.detail}; the message waits and goes out once someone is` : c.detail;
  button.title = `${CONNECTION_TEXT[c.state]}: ${text}`;
  button.setAttribute("aria-label", `Send, ${CONNECTION_TEXT[c.state]}`);
  document.getElementById("compose-connection").textContent = CONNECTION_TEXT[c.state];
}

updateSendButton().catch(() => {});
setInterval(() => {
  if (document.getElementById("messages").classList.contains("active") && !document.hidden) {
    updateSendButton().catch(() => {});  // the local app is always reachable; try again next time
  }
}, CONNECTION_REFRESH_MS);

// Pick up messages that arrive or get delivered in the background (sync, other devices)
const MESSAGES_REFRESH_MS = 1000;
setInterval(() => {
  if (document.getElementById("messages").classList.contains("active") && !document.hidden) {
    loadMessages().catch(() => {});  // the local app is always reachable; try again next time
  }
}, MESSAGES_REFRESH_MS);

// Hide/show the chats sidebar (handy on narrow phone screens); remembered per browser
const CHATS_HIDDEN_KEY = "chatsHidden";

function setChatsHidden(hidden) {
  document.getElementById("chat-sidebar").hidden = hidden;
  const toggle = document.getElementById("chat-sidebar-toggle");
  toggle.setAttribute("aria-expanded", String(!hidden));
  toggle.title = toggle.ariaLabel = hidden ? "Show chats" : "Hide chats";
  try { localStorage.setItem(CHATS_HIDDEN_KEY, hidden ? "1" : ""); } catch {}
}

document.getElementById("chat-sidebar-toggle").addEventListener("click", () =>
  setChatsHidden(!document.getElementById("chat-sidebar").hidden));

setChatsHidden((() => {
  try { return localStorage.getItem(CHATS_HIDDEN_KEY) === "1"; } catch { return false; }
})());
