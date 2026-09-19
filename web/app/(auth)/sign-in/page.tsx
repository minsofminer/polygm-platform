import type { Metadata } from "next";
import { SignInForm } from "@/auth/SignInForm";

export const metadata: Metadata = { title: "Sign in", robots: { index: false } };

export default function SignInPage() {
  return <SignInForm />;
}
