const API_OVERRIDE = new URLSearchParams(window.location.search).get("api");
const API = (API_OVERRIDE || window.CAMPUS_CAFE_CONFIG?.apiBaseUrl || "").replace(/\/$/, "");
const cart = new Map();
const knownOrderIds = new Set(JSON.parse(localStorage.getItem("campusCafeOrderIds") || "[]"));
const symbols = ["☕", "◉", "✿", "▧", "❋", "◌"];
let products = [];

const money = cents => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(cents / 100);
const escapeHtml = text => String(text).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const status = (online, label) => {
  const el = document.querySelector(".api-status");
  el.className = `api-status ${online ? "online" : "offline"}`;
  document.querySelector("#status-text").textContent = label;
};

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || `Request failed (${response.status})`);
  return { data, response };
}

async function loadProducts() {
  if (!API || API.includes("replace-with-api-id")) {
    status(false, "Set API URL in config.js");
    document.querySelector("#products").innerHTML = '<p class="empty-note">Add the deployed API URL to frontend/config.js to load the menu.</p>';
    return;
  }
  try {
    const { data } = await api("/products");
    products = data.items || [];
    status(true, "Menu is fresh");
    renderProducts();
  } catch (error) {
    status(false, "Menu unavailable");
    document.querySelector("#products").innerHTML = `<p class="empty-note">${escapeHtml(error.message)}</p>`;
  }
}

function renderProducts() {
  document.querySelector("#product-count").textContent = `${products.length} fresh picks`;
  document.querySelector("#products").innerHTML = products.map((product, index) => `
    <article class="product-card">
      <div class="product-top"><span class="product-symbol" aria-hidden="true">${symbols[index % symbols.length]}</span>${product.available ? "" : '<span class="unavailable">SOLD OUT</span>'}</div>
      <div><h3>${escapeHtml(product.name)}</h3><p>${product.available ? "A campus favorite, made fresh." : "Back on the menu soon."}</p></div>
      <div class="product-bottom"><span class="price">${money(product.priceCents)}</span><button class="add-button" data-add="${escapeHtml(product.productId)}" ${product.available ? "" : "disabled"}>Add to order ＋</button></div>
    </article>`).join("");
  document.querySelectorAll("[data-add]").forEach(button => button.addEventListener("click", () => addToCart(button.dataset.add)));
}

function addToCart(productId) {
  const product = products.find(item => item.productId === productId);
  const current = cart.get(productId)?.quantity || 0;
  if (!product?.available || current >= 10 || (current === 0 && cart.size >= 10)) return;
  cart.set(productId, { product, quantity: current + 1 });
  renderCart();
}

function setQuantity(productId, quantity) {
  if (quantity <= 0) cart.delete(productId);
  else if (quantity <= 10) cart.get(productId).quantity = quantity;
  renderCart();
}

function renderCart() {
  const entries = [...cart.entries()];
  const list = document.querySelector("#cart-items");
  list.innerHTML = entries.length ? entries.map(([id, { product, quantity }]) => `
    <div class="cart-row"><div><div class="cart-product-name">${escapeHtml(product.name)}</div><div class="cart-controls"><button class="quantity-button" data-minus="${escapeHtml(id)}" aria-label="Decrease quantity">−</button><span>${quantity}</span><button class="quantity-button" data-plus="${escapeHtml(id)}" aria-label="Increase quantity">+</button></div></div><strong>${money(product.priceCents * quantity)}</strong></div>`).join("") : '<p class="empty-note">Your next favorite is waiting.</p>';
  document.querySelectorAll("[data-minus]").forEach(button => button.addEventListener("click", () => setQuantity(button.dataset.minus, cart.get(button.dataset.minus).quantity - 1)));
  document.querySelectorAll("[data-plus]").forEach(button => button.addEventListener("click", () => setQuantity(button.dataset.plus, cart.get(button.dataset.plus).quantity + 1)));
  const total = entries.reduce((sum, [, item]) => sum + item.product.priceCents * item.quantity, 0);
  document.querySelector("#cart-total").textContent = money(total);
  document.querySelector("#submit-order").disabled = entries.length === 0;
}

function orderRequest() {
  return { items: [...cart.entries()].map(([productId, item]) => ({ productId, quantity: item.quantity })) };
}

async function submitOrder() {
  const feedback = document.querySelector("#order-feedback");
  feedback.textContent = "";
  const body = orderRequest();
  const fingerprint = JSON.stringify([...body.items].sort((a, b) => a.productId.localeCompare(b.productId)));
  let pending = JSON.parse(sessionStorage.getItem("campusCafePending") || "null");
  if (!pending || pending.fingerprint !== fingerprint) {
    pending = { fingerprint, key: crypto.randomUUID() };
    sessionStorage.setItem("campusCafePending", JSON.stringify(pending));
  }
  const button = document.querySelector("#submit-order");
  button.disabled = true;
  button.textContent = "Sending…";
  try {
    const { data } = await api("/orders", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": pending.key }, body: JSON.stringify(body) });
    knownOrderIds.add(data.orderId);
    localStorage.setItem("campusCafeOrderIds", JSON.stringify([...knownOrderIds]));
    sessionStorage.removeItem("campusCafePending");
    cart.clear();
    renderCart();
    feedback.style.color = "#3b7452";
    feedback.textContent = `Order placed · ${data.orderId}`;
    await loadOrder(data.orderId);
  } catch (error) {
    feedback.style.color = "#a34736";
    feedback.textContent = `${error.message} Retry keeps the same key for this cart.`;
  } finally {
    button.innerHTML = 'Place order <span aria-hidden="true">→</span>';
    button.disabled = cart.size === 0;
  }
}

function renderOrder(order) {
  const card = document.createElement("article");
  card.className = "order-card";
  card.dataset.orderId = order.orderId;
  const statusClass = order.status === "CANCELLED" ? "cancelled" : "";
  card.innerHTML = `<div class="order-card-header"><h3>Order ${escapeHtml(order.orderId.slice(0, 8))}…</h3><span class="status-pill ${statusClass}">${escapeHtml(order.status)}</span></div><p class="order-meta">${new Date(order.createdAt).toLocaleString()}</p><div class="order-lines">${order.items.map(item => `${item.quantity} × ${escapeHtml(item.name)} · ${money(item.unitPriceCents)}`).join("<br>")}</div><div class="order-bottom"><span>${money(order.totalCents)}</span>${order.status === "CREATED" ? `<button class="cancel-button" data-cancel="${escapeHtml(order.orderId)}">Cancel order</button>` : ""}</div>`;
  document.querySelector(`[data-order-id="${CSS.escape(order.orderId)}"]`)?.remove();
  document.querySelector("#orders-list").prepend(card);
  card.querySelector("[data-cancel]")?.addEventListener("click", () => cancelOrder(order.orderId));
}

async function loadOrder(orderId) {
  try {
    const { data } = await api(`/orders/${encodeURIComponent(orderId)}`);
    knownOrderIds.add(data.orderId);
    localStorage.setItem("campusCafeOrderIds", JSON.stringify([...knownOrderIds]));
    renderOrder(data);
    return data;
  } catch (error) {
    const feedback = document.querySelector("#order-feedback");
    feedback.textContent = error.message;
  }
}

async function cancelOrder(orderId) {
  try {
    const { data } = await api(`/orders/${encodeURIComponent(orderId)}/cancel`, { method: "POST" });
    renderOrder(data);
  } catch (error) {
    window.alert(error.message);
  }
}

document.querySelector("#submit-order").addEventListener("click", submitOrder);
document.querySelector("#lookup-form").addEventListener("submit", async event => {
  event.preventDefault();
  const orderId = document.querySelector("#order-id-input").value.trim();
  if (orderId) await loadOrder(orderId);
});

loadProducts();
Promise.all([...knownOrderIds].map(loadOrder));

