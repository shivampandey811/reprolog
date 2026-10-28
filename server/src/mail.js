// Minimal SMTP client for sending sign-in codes through Gmail (smtp.gmail.com:465, TLS).
// Needs SMTP_USER (the Gmail address) and SMTP_PASS (a Google app password, which
// requires 2-step verification on that Google account).
import { connect } from "cloudflare:sockets";

export async function sendMail(env, { to, subject, text }) {
  if (!env.SMTP_USER || !env.SMTP_PASS) throw new Error("SMTP_USER / SMTP_PASS are not set");
  const host = env.SMTP_HOST || "smtp.gmail.com";
  const from = env.MAIL_FROM || env.SMTP_USER;
  const socket = connect({ hostname: host, port: Number(env.SMTP_PORT || 465) }, { secureTransport: "on" });
  const writer = socket.writable.getWriter();
  const reader = socket.readable.getReader();
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  let buffer = "";

  // Reads one (possibly multi-line) reply, e.g. "250-..." lines ending with "250 ...".
  async function reply(expect) {
    for (;;) {
      const lines = buffer.split("\r\n");
      for (let i = 0; i < lines.length - 1; i++) {
        if (/^\d{3} /.test(lines[i])) {
          buffer = lines.slice(i + 1).join("\r\n");
          const code = Number(lines[i].slice(0, 3));
          if (!expect.includes(code)) throw new Error(`SMTP ${lines[i]}`);
          return code;
        }
      }
      const { value, done } = await reader.read();
      if (done) throw new Error("SMTP connection closed");
      buffer += dec.decode(value, { stream: true });
    }
  }
  const send = (line) => writer.write(enc.encode(line + "\r\n"));
  const b64 = (s) => btoa(unescape(encodeURIComponent(s)));

  try {
    await reply([220]);
    await send("EHLO reprolog");
    await reply([250]);
    await send("AUTH LOGIN");
    await reply([334]);
    await send(b64(env.SMTP_USER));
    await reply([334]);
    await send(b64(env.SMTP_PASS));
    await reply([235]);
    await send(`MAIL FROM:<${env.SMTP_USER}>`);
    await reply([250]);
    await send(`RCPT TO:<${to}>`);
    await reply([250, 251]);
    await send("DATA");
    await reply([354]);
    const message = [
      `From: reprolog <${from}>`,
      `To: <${to}>`,
      `Subject: =?UTF-8?B?${b64(subject)}?=`,
      `Date: ${new Date().toUTCString()}`,
      `Message-ID: <${crypto.randomUUID()}@reprolog>`,
      "MIME-Version: 1.0",
      "Content-Type: text/plain; charset=UTF-8",
      "Content-Transfer-Encoding: base64",
      "",
      b64(text).replace(/.{76}/g, "$&\r\n"),
      ".",
    ].join("\r\n");
    await send(message);
    await reply([250]);
    await send("QUIT");
  } finally {
    await writer.close().catch(() => {});
    await socket.close().catch(() => {});
  }
}
