import React from "react";
import LegalLayout from "./LegalLayout";

const sections = [
  ["Who we are", "FacelessForge is operated by ETHINX Systems in Adelaide, South Australia, at facelessforge.ethinx.solutions."],
  ["What we collect", "Your name, email address and password (stored only as a hash). The topics, scripts, videos and thumbnails you create. Billing details from our payment provider: your plan, email and transaction ID. We never see your card number."],
  ["YouTube", "If you connect YouTube, we ask for permission to upload videos to your channel. We use it only to upload videos you generated, when you ask or on a schedule you turn on. We do not read your watch history, subscriptions or other account data. You can disconnect in the app at any time, and you can remove access at myaccount.google.com/permissions."],
  ["Google API Services User Data Policy", "FacelessForge use and transfer of information received from Google APIs adheres to the Google API Services User Data Policy, including the Limited Use requirements. We do not sell this data, use it for advertising, or share it except as needed to provide the service."],
  ["Service providers", "We use Hetzner (Germany) for hosting, Cloudflare for file storage, ElevenLabs for voice generation, Pexels for stock footage, AI services for writing, and Dodo Payments for payments. Each receives only what it needs to do its job."],
  ["Keeping and deleting your data", "Your projects stay until you delete them. Rendered videos are removed automatically after a short period. To delete your account and data, email us at the address below and we will act within 30 days."],
  ["Contact", "tdogga14@gmail.com"],
];

export default function PrivacyPage() {
  return <LegalLayout title="Privacy policy" updated="3 October 2026" sections={sections} />;
}
