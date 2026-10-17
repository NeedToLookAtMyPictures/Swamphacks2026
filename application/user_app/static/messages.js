// Messages tab: one conversation per person in a sidebar (Everyone pinned to the
// top); clicking one shows its history and sends new messages to that person.

let myName = null;
let selected = null;   // receiver of the open conversation: BROADCAST or a name

const BROADCAST = "*";        // receiver for messages to everyone

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
  // grey out my own messages once they've been delivered or expired
  if (m.is_dead && outgoing) card.classList.add("dead");

  const head = msgEl("div", undefined, "message-head");
  head.appendChild(msgEl("strong", outgoing ? "You" : m.sender));
  head.appendChild(msgEl("span", new Date(m.time_created * 1000).toLocaleString()));
  card.appendChild(head);
  card.appendChild(msgEl("div", m.content, "message-body"));

  if (m.is_dead && outgoing) {
    card.appendChild(msgEl("div", broadcast ? "expired" : "delivered or expired", "message-note"));
  }
  return card;
}

let lastMessages = [];
let lastPeople = [];

async function loadMessages() {
  const list = document.getElementById("message-list");
  const deviceRes = await fetch("/api/device");
  if (!deviceRes.ok) {
    document.getElementById("chat-list").replaceChildren();
    document.getElementById("chat-title").textContent = "";
    list.replaceChildren(msgEl("p", "Set up this device first (Debug tab).", "message-note"));
    return;
  }
  myName = (await deviceRes.json()).name;

  const [messages, contacts, gps] = await Promise.all([
    fetch("/api/messages").then((r) => r.json()),
    fetch("/api/contacts").then((r) => r.json()),
    fetch("/api/geolocations").then((r) => r.json()),
  ]);

  // Suggestions for the "New chat" box: everyone this device knows about
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

  // One conversation per person I've messaged with or seen on GPS
  const latest = new Map();  // lowercase name -> {name, time}
  const note = (name, time) => {
    if (!name || name === BROADCAST || sameName(name, myName)) return;
    const key = name.toLowerCase();
    const prev = latest.get(key);
    if (!prev || time > prev.time) latest.set(key, { name: prev ? prev.name : name, time });
  };
  gps.forEach((g) => note(g.user, 0));  // seen but never messaged: listed after conversations
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
  document.getElementById("compose-status").textContent = "";
  render();
}

document.getElementById("new-chat").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("new-chat-name");
  const name = input.value.trim();
  if (!name) return;
  input.value = "";
  if (sameName(name, myName)) return;
  const existing = lastPeople.find((p) => sameName(p, name));
  if (!existing) lastPeople.unshift(name);
  openChat(existing || name);
});

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
    status.textContent = `Queued for ${label}. It will be passed along as you meet people.`;
    content.value = "";
    loadMessages();
  } else {
    status.textContent = (await res.json()).error;
  }
});
