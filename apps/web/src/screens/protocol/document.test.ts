import { describe, expect, test } from "vite-plus/test";

import { blankDoc, startDraft } from "./document.ts";

describe("startDraft", () => {
  const labelled = (version_label: string) => ({ ...blankDoc(), version_label });

  test("gives a draft of the version in force the next label", () => {
    expect(startDraft(labelled("2"), "2").version_label).toBe("3");
    expect(startDraft(labelled("1.1"), "1.2").version_label).toBe("1.3");
  });

  test("keeps a label that already comes after the version in force", () => {
    expect(startDraft(labelled("3"), "2").version_label).toBe("3");
  });

  test("keeps the label before the first publication", () => {
    expect(startDraft(labelled("0.1"), null).version_label).toBe("0.1");
  });
});
