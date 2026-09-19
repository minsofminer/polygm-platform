import { NextResponse } from "next/server";
import { tear } from "@/auth/server";

export async function POST() {
  await tear();
  return NextResponse.json({ ended: true });
}
