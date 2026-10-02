// Pro waitlist: one private blob per signup in the project's Vercel Blob store.
import { randomUUID } from "node:crypto";
import { put } from "@vercel/blob";

const EMAIL = /^[^\s@]{1,64}@[^\s@]{1,189}\.[^\s@]{2,}$/;

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).send("Method not allowed");
  }
  const body = typeof req.body === "string" ? Object.fromEntries(new URLSearchParams(req.body)) : req.body || {};
  if (body.company) return res.redirect(303, "/joined"); // honeypot: bots fill the hidden field
  const email = String(body.email || "").trim().toLowerCase();
  if (email.length > 254 || !EMAIL.test(email)) {
    return res.status(400).send("That email address doesn't look right. Go back and try again.");
  }
  await put(`waitlist/${new Date().toISOString().slice(0, 10)}/${randomUUID()}.json`,
    JSON.stringify({ email, at: new Date().toISOString() }),
    { access: "private", contentType: "application/json" });
  return res.redirect(303, "/joined");
}
