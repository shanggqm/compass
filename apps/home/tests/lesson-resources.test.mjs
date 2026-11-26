import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, mkdir, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { prepareLessonResources } from "../scripts/lesson-resources.mjs";

const app = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const lessons = JSON.parse(await readFile(path.join(app, "src/lib/lesson-resources.json"), "utf8"));

test("course resources list lessons 11–26 in order", () => {
  assert.deepEqual(
    lessons.map((lesson) => lesson.number),
    [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26],
  );
});

for (const lesson of lessons) {
  test(`lesson ${lesson.number} editions exist and are included in cache inputs`, async () => {
    const turbo = JSON.parse(await readFile(path.join(app, "turbo.json"), "utf8"));
    for (const artifact of lesson.artifacts) {
      const source = await readFile(path.resolve(app, "../..", artifact.source), "utf8");
      assert.match(source, /<!doctype html>/i);
      assert.match(
        artifact.href,
        new RegExp(`^/resources/lessons/${lesson.number}/[\\w-]+\\.html$`),
      );
      for (const task of ["build", "test"])
        assert.ok(turbo.tasks[task].inputs.includes(`$TURBO_ROOT$/${artifact.source}`));
      if (artifact.markdown) {
        const { source, href } = artifact.markdown;
        assert.match(await readFile(path.resolve(app, "../..", source), "utf8"), /^# /);
        assert.equal(href, artifact.href.replace(/\.html$/, ".md"));
        for (const task of ["build", "test"])
          assert.ok(turbo.tasks[task].inputs.includes(`$TURBO_ROOT$/${source}`));
      }
    }
  });
}

test("distribution preserves multiple artifacts per lesson and rejects missing sources", async () => {
  const fixture = await mkdtemp(path.join(tmpdir(), "compass-lessons-"));
  try {
    await mkdir(path.join(fixture, "src/lib"), { recursive: true });
    await mkdir(path.join(fixture, "reference/lessons/11"), { recursive: true });
    const artifacts = ["design", "appendix"].map((id) => ({
      id,
      source: `design/${id.toUpperCase()}.html`,
      href: `/resources/lessons/11/${id}.html`,
    }));
    artifacts[0].markdown = { source: "design/DESIGN.md", href: "/resources/lessons/11/design.md" };
    const markdown = "# Design\n\n[Appendix](APPENDIX.html#details)\n";
    const markdownSource = path.join(fixture, "reference/lessons/11/design.md");
    const markdownDestination = path.join(fixture, "public/resources/lessons/11/design.md");
    await writeFile(
      path.join(fixture, "src/lib/lesson-resources.json"),
      JSON.stringify([{ number: 11, artifacts }]),
    );
    for (const artifact of artifacts)
      await writeFile(
        path.join(fixture, "reference/lessons/11", `${artifact.id}.html`),
        `<html><head></head>${artifact.id}<a href="APPENDIX.html#details">Appendix</a></html>`,
      );
    await writeFile(markdownSource, markdown);
    await prepareLessonResources(fixture);
    for (const artifact of artifacts)
      assert.match(
        await readFile(path.join(fixture, "public", artifact.href), "utf8"),
        /href="appendix\.html#details"/,
      );
    assert.equal(await readFile(markdownDestination, "utf8"), markdown);
    await rm(markdownSource);
    await assert.rejects(prepareLessonResources(fixture), /Missing lesson resource: .*design\.md/);
    assert.match(
      await readFile(path.join(fixture, "public/resources/lessons/11/design.html"), "utf8"),
      /home-reader-overrides/,
    );
  } finally {
    await rm(fixture, { recursive: true, force: true });
  }
});

test("single lesson publication preserves other lessons and distributes linked editions", async () => {
  const fixture = await mkdtemp(path.join(tmpdir(), "compass-lesson-publish-"));
  try {
    const source = path.join(fixture, "reference/lessons/23");
    const previous = path.join(fixture, "public/resources/lessons/22");
    const current = path.join(fixture, "public/resources/lessons/23");
    await mkdir(source, { recursive: true });
    await mkdir(previous, { recursive: true });
    await mkdir(current, { recursive: true });
    await writeFile(path.join(current, "retired.html"), "retired edition");
    await writeFile(path.join(current, "retired.md"), "# Retired edition");
    await mkdir(path.join(fixture, "src/lib"), { recursive: true });
    await writeFile(path.join(previous, "technical-design.html"), "previous edition");
    const lesson = (number) => ({
      number,
      artifacts: [
        { source: "TECH_DESIGN.html", href: `/resources/lessons/${number}/technical-design.html` },
        { source: "VECTOR_SEARCH.html", href: `/resources/lessons/${number}/vector-search.html` },
      ],
    });
    await writeFile(
      path.join(fixture, "src/lib/lesson-resources.json"),
      JSON.stringify([lesson(22), lesson(23)]),
    );
    await writeFile(
      path.join(source, "technical-design.html"),
      '<html><head></head><a href="VECTOR_SEARCH.html#section-2">专题</a><a href="RETIRED.html">旧专题</a></html>',
    );
    await writeFile(path.join(source, "vector-search.html"), "<html><head></head></html>");
    await prepareLessonResources(fixture, 23);
    assert.equal(
      await readFile(path.join(previous, "technical-design.html"), "utf8"),
      "previous edition",
    );
    const published = await readFile(
      path.join(fixture, "public/resources/lessons/23/technical-design.html"),
      "utf8",
    );
    assert.match(published, /href="vector-search\.html#section-2"/);
    assert.match(published, /旧专题/);
    assert.doesNotMatch(published, /href="RETIRED\.html"/);
    await assert.rejects(readFile(path.join(current, "retired.html")), { code: "ENOENT" });
    await assert.rejects(readFile(path.join(current, "retired.md")), { code: "ENOENT" });
    await assert.rejects(prepareLessonResources(fixture, 99), /Unknown lesson/);
  } finally {
    await rm(fixture, { recursive: true, force: true });
  }
});
