// Messages tab: send to someone you've encountered or to any name, and list messages.

let myName = null;

const BROADCAST = "*";        // receiver for messages to everyone
const OTHER = "__other__";    // dropdown value that reveals the free-text box

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

function renderMessage(m) {
  const outgoing = sameName(m.sender, myName);
  const broadcast = m.receiver === BROADCAST;
  const card = msgEl("div", undefined, `message ${outgoing ? "outgoing" : "incoming"}`);
  if (m.is_dead) card.classList.add("dead");

  const head = msgEl("div", undefined, "message-head");
  const to = broadcast ? "Everyone" : m.receiver;
  head.appendChild(msgEl("strong", outgoing ? `To ${to}` : `From ${m.sender}${broadcast ? " to Everyone" : ""}`));
  head.appendChild(msgEl("span", new Date(m.time_created * 1000).toLocaleString()));
  card.appendChild(head);
  card.appendChild(msgEl("div", m.content, "message-body"));

  if (m.is_dead) {
    card.appendChild(msgEl("div", outgoing ? "delivered or expired" : "delivered", "message-note"));
  } else if (!outgoing && !broadcast) {
    const button = msgEl("button", "Mark delivered");
    button.addEventListener("click", async () => {
      await fetch(`/api/messages/${encodeURIComponent(m.origin_id)}/${m.seq}/delivered`,
                  { method: "POST" });
      loadMessages();
    });
    card.appendChild(button);
  }
  return card;
}

async function loadMessages() {
  const list = document.getElementById("message-list");
  const deviceRes = await fetch("/api/device");
  if (!deviceRes.ok) {
    list.replaceChildren(msgEl("p", "Set up this device first (Debug tab).", "message-note"));
    return;
  }
  myName = (await deviceRes.json()).name;

  const [messages, contacts, gps] = await Promise.all([
    fetch("/api/messages").then((r) => r.json()),
    fetch("/api/contacts").then((r) => r.json()),
    fetch("/api/geolocations").then((r) => r.json()),
  ]);

  fillRecipients(gps.filter((g) => !sameName(g.user, myName)));

  // Suggestions for the "Type a name" box: everyone this device knows about
  const options = document.getElementById("contact-list");
  options.replaceChildren(...contacts.map((c) => {
    const option = msgEl("option");
    option.value = c.name;
    option.label = describeContact(c);
    return option;
  }));

  // Only messages I sent or that are addressed to me; the rest are just being relayed
  const mine = messages
    .filter((m) => m.receiver === BROADCAST || [m.sender, m.receiver].some((n) => sameName(n, myName)))
    .reverse();  // newest first
  list.replaceChildren(...(mine.length ? mine.map(renderMessage)
                                       : [msgEl("p", "No messages yet.", "message-note")]));
}

// "To" dropdown: everyone in the GPS table (newest position first), Everyone, or Other
function fillRecipients(people) {
  const select = document.getElementById("compose-to");
  const previous = select.value;
  const option = (value, label) => {
    const o = msgEl("option", label);
    o.value = value;
    return o;
  };
  const sorted = [...people].sort((a, b) => b.time_created - a.time_created);
  const placeholder = option("", "Choose a recipient…");
  placeholder.disabled = true;
  select.replaceChildren(
    placeholder,
    option(BROADCAST, "Everyone"),
    ...sorted.map((g) => option(g.user,
      `${g.user} (seen ${new Date(g.time_created * 1000).toLocaleString()})`)),
    option(OTHER, "Other (type a name)…"),
  );
  // Keep the current choice across refreshes if it's still there
  select.value = [...select.options].some((o) => o.value === previous && previous) ? previous : "";
  toggleOther();
}

function toggleOther() {
  const other = document.getElementById("compose-other");
  other.hidden = document.getElementById("compose-to").value !== OTHER;
  other.required = !other.hidden;
}

document.getElementById("compose-to").addEventListener("change", toggleOther);

document.getElementById("compose").addEventListener("submit", async (e) => {
  e.preventDefault();
  const select = document.getElementById("compose-to");
  const receiver = select.value === OTHER
    ? document.getElementById("compose-other").value.trim()
    : select.value;
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
