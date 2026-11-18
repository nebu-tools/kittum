// The project's one protocol draft, on the real stack (see stack.ts), open on two pages of one
// lead at once. Each page saves as the lead types; a page that saves on text another page has
// since changed is told so, and either loads the newer draft or keeps its own text and saves it.
import { execFileSync } from "node:child_process";

import { type Browser, expect, type Page, test } from "@playwright/test";

import { SERVER, stackEnv } from "./stack.ts";

const cli = (...args: string[]) =>
  JSON.parse(
    execFileSync("uv", ["run", "kittum", "project", ...args], {
      cwd: SERVER,
      env: stackEnv(),
      encoding: "utf8",
    }),
  ) as Record<string, unknown>;

/** A page with the protocol's YAML open, as a lead of a project with nothing published. */
const open = async (browser: Browser, url: string): Promise<Page> => {
  const page = await (await browser.newContext()).newPage();
  await page.goto(url);
  const yaml = page.getByRole("tab", { name: "YAML" });
  if ((await yaml.getAttribute("aria-selected")) !== "true") await yaml.click();
  await expect(page.getByRole("textbox", { name: "Protocol", exact: true })).toBeVisible();
  return page;
};

const editor = (page: Page) => page.getByRole("textbox", { name: "Protocol", exact: true });

test("two pages on one draft: a stale save is caught, and either text can win", async ({
  browser,
}) => {
  const lead = cli("create", `Draft ${new Date().toISOString()}`, "--lead", "Morgan Lee") as {
    project_id: string;
    lead: string;
  };
  const url = `/p/${lead.project_id}/protocol?as=${lead.lead}`;

  const a = await open(browser, url);
  await editor(a).fill("title: written on page A");
  await expect(a.getByTestId("draft-status")).toHaveText("Draft saved");

  // The second page opens the draft the first one saved, and saves over it.
  const b = await open(browser, url);
  await expect(editor(b)).toHaveValue("title: written on page A");
  await editor(b).fill("title: written on page B");
  await expect(b.getByTestId("draft-status")).toHaveText("Draft saved");

  // The first page still holds the old revision: its save is refused, and nothing is lost.
  await editor(a).fill("title: page A again");
  const conflict = a.getByTestId("draft-conflict");
  await expect(conflict).toContainText("The draft changed elsewhere since you last saved.");
  await conflict.getByRole("button", { name: "Keep mine as a copy in the editor" }).click();
  await expect(editor(a)).toHaveValue("title: page A again");
  await expect(a.getByTestId("draft-status")).toHaveText("Draft not saved");
  await a.getByRole("button", { name: "Save now" }).click();
  await expect(a.getByTestId("draft-status")).toHaveText("Draft saved");

  // Now the second page is behind, and takes the newer draft.
  await editor(b).fill("title: page B again");
  const behind = b.getByTestId("draft-conflict");
  await expect(behind).toContainText("The draft changed elsewhere since you last saved.");
  await behind.getByRole("button", { name: "Load the newer draft" }).click();
  await expect(editor(b)).toHaveValue("title: page A again");
  await expect(b.getByTestId("draft-status")).toHaveText("Draft saved");

  // A page opened afresh reads what was saved last.
  const c = await open(browser, url);
  await expect(editor(c)).toHaveValue("title: page A again");
});
