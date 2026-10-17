// Messages tab: send to someone you've encountered or to any name, and list messages.

let myName = null;

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
  const outgoing = m.sender.toLowerCase() === myName.toLowerCase();
  const card = msgEl("div", undefined, `message ${outgoing ? "outgoing" : "incoming"}`);
  if (m.is_dead) card.classList.add("dead");

  const head = msgEl("div", undefined, "message-head");
  head.appendChild(msgEl("strong", outgoing ? `To ${m.receiver}` : `From ${m.sender}`));
  head.appendChild(msgEl("span", new Date(m.time_created * 1000).toLocaleString()));
  card.appendChild(head);
  card.appendChild(msgEl("div", m.content, "message-body"));

  if (m.is_dead) {
    card.appendChild(msgEl("div", outgoing ? "delivered or expired" : "delivered", "message-note"));
  } else if (!outgoing) {
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

  const [messages, contacts] = await Promise.all([
    fetch("/api/messages").then((r) => r.json()),
    fetch("/api/contacts").then((r) => r.json()),
  ]);

  // Suggestions for the "To" box: everyone this device knows about
  const options = document.getElementById("contact-list");
  options.replaceChildren(...contacts.map((c) => {
    const option = msgEl("option");
    option.value = c.name;
    option.label = describeContact(c);
    return option;
  }));

  // Only messages I sent or that are addressed to me; the rest are just being relayed
  const mine = messages
    .filter((m) => [m.sender, m.receiver].some((n) => n.toLowerCase() === myName.toLowerCase()))
    .reverse();  // newest first
  list.replaceChildren(...(mine.length ? mine.map(renderMessage)
                                       : [msgEl("p", "No messages yet.", "message-note")]));
}

document.getElementById("compose").addEventListener("submit", async (e) => {
  e.preventDefault();
  const to = document.getElementById("compose-to");
  const content = document.getElementById("compose-content");
  const status = document.getElementById("compose-status");
  const res = await fetch("/api/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ receiver: to.value.trim(), content: content.value }),
  });
  if (res.ok) {
    status.textContent = `Queued for ${to.value.trim()}. It will be passed along as you meet people.`;
    content.value = "";
    loadMessages();
  } else {
    status.textContent = (await res.json()).error;
  }
});
