import React from "react";
import LegalLayout from "./LegalLayout";

const sections = [
  ["1. The service", "FacelessForge generates scripts, voiceovers, thumbnails and short videos. You review content before it is published. Videos are uploaded to YouTube only if you connect your channel and turn on uploading."],
  ["2. Your account", "Keep your login secure. You are responsible for activity on your account. We may suspend accounts that break these terms."],
  ["3. Billing and refunds", "Plans and credits are sold through Dodo Payments. You are charged only when you complete checkout. Credits are used as you render videos. You can ask for a refund of unused credits within 14 days of purchase. Used credits are not refundable. Nothing in these terms limits your rights under the Australian Consumer Law."],
  ["4. Acceptable use", "Do not create unlawful, hateful, sexual, self-harm or infringing content. You must follow the YouTube Terms of Service and the YouTube API Services Terms. You are responsible for the content you publish and for checking stock footage licences."],
  ["5. Ownership", "You keep the rights to the content you create. We keep the rights to the software."],
  ["6. Liability", "The service is provided as is. To the extent the law allows, we are not liable for demonetisation, copyright strikes or lost revenue, and our liability is limited to the fees you paid in the last 30 days. The law of South Australia applies."],
  ["Contact", "tdogga14@gmail.com"],
];

export default function TermsPage() {
  return <LegalLayout title="Terms of service" updated="3 October 2026" sections={sections} />;
}
