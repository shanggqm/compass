"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { AnalysisMarkdown } from "./analysis-markdown";
import { useDocumentSession } from "./shell/session-boundary";
import { Modal } from "./ui/dialog";
import type { components } from "@/generated/api";
import { parseModelContract } from "@/contracts/model-contract";

type Index = components["schemas"]["RunCitations"];
type Detail = components["schemas"]["CitationDetail"];
type Message = components["schemas"]["MessageSnapshot"];

export async function validAnchors(source: string, message: Message, index: Index) {
  if (
    !index.anchors.length ||
    (message.status !== "completed" && message.status !== "incomplete") ||
    index.messageId !== message.messageId ||
    index.revision !== message.revision ||
    message.content.text !== source
  )
    return false;
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(source));
  const digest = Array.from(new Uint8Array(bytes), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
  if (digest !== index.textDigest) return false;
  const chars = Array.from(source);
  let end = 0;
  return index.anchors.every((anchor) => {
    if (anchor.start < end || anchor.end > chars.length || anchor.end <= anchor.start) return false;
    end = anchor.end;
    return [`[${anchor.ordinal}]`, `^[${anchor.ordinal}]^`].includes(
      chars.slice(anchor.start, end).join(""),
    );
  });
}

const statusText = {
  supported: "已核实",
  contested: "来源有分歧",
  insufficient: "材料待补",
  refuted: "已由反证纠正",
};

export function CitedAnswer({
  source,
  message,
  runId,
  scope,
  children,
}: {
  source: string;
  message: Message;
  runId: string;
  scope: { tenantId: string; workspaceId: string };
  children: ReactNode;
}) {
  const api = useDocumentSession();
  const query = new URLSearchParams({
    tenantId: scope.tenantId,
    workspaceId: scope.workspaceId,
  }).toString();
  const path = "analysis/runs/" + runId + "/citations";
  const [index, setIndex] = useState<Index>();
  const [selected, setSelected] = useState<Index["anchors"][number]>();
  const [detail, setDetail] = useState<Detail>();
  const [error, setError] = useState(false);
  const opener = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    void api
      .request(path + "?" + query, "GET", undefined, { signal: ctl.signal, retainOnNotFound: true })
      .then(async (raw) => {
        const saved = parseModelContract<Index>("RunCitations", raw);
        if ((await validAnchors(source, message, saved)) && !ctl.signal.aborted) setIndex(saved);
      })
      .catch(() => {});
    const clear = api.onClear(() => {
      ctl.abort();
      setIndex(undefined);
      setSelected(undefined);
      setDetail(undefined);
    });
    return () => {
      ctl.abort();
      clear();
    };
  }, [api, path, query, source, message]);
  useEffect(() => {
    if (!selected) return;
    const ctl = new AbortController();
    void api
      .request(path + "/" + selected.citationId + "?" + query, "GET", undefined, {
        signal: ctl.signal,
        retainOnNotFound: true,
      })
      .then((raw) => {
        const current = parseModelContract<Detail>("CitationDetail", raw);
        if (current.citationId !== selected.citationId) throw Error();
        if (!ctl.signal.aborted) setDetail(current);
      })
      .catch(() => {
        if (!ctl.signal.aborted) setError(true);
      });
    return () => ctl.abort();
  }, [api, path, query, selected]);
  if (!index || !index.anchors.length) return children;
  const chars = Array.from(source);
  let markdown = "";
  const citationLinks = new Map<string, ReactNode>();
  let cursor = 0;
  for (const anchor of index.anchors) {
    const key = `#compass-citation-${anchor.citationId}-${anchor.start}`;
    markdown += chars.slice(cursor, anchor.start).join("") + `[${anchor.ordinal}](${key})`;
    citationLinks.set(
      key,
      <button
        type="button"
        className="analysis-citation"
        aria-label={"查看引用 " + anchor.ordinal}
        onClick={(event) => {
          opener.current = event.currentTarget;
          setError(false);
          setDetail(undefined);
          setSelected(anchor);
        }}
      >
        [{anchor.ordinal}]
      </button>,
    );
    cursor = anchor.end;
  }
  markdown += chars.slice(cursor).join("");
  return (
    <>
      <AnalysisMarkdown source={markdown} citationLinks={citationLinks} />
      {selected && (
        <Modal
          title={"引用 " + selected.ordinal}
          open
          onClose={() => setSelected(undefined)}
          returnFocus={() => opener.current}
        >
          {error ? (
            <p role="alert">当前无法查看这条引用。</p>
          ) : !detail ? (
            <p role="status">正在核对来源权限…</p>
          ) : (
            <>
              <p>
                <strong>{statusText[detail.status]}</strong>
              </p>
              <p>{detail.statement}</p>
              {detail.calculation && <p>{detail.calculation}</p>}
              {detail.sources.map((source, i) => (
                <section className="analysis-citation-source" key={i}>
                  <h3>{source.title}</h3>
                  <p>
                    {source.version && "版本 " + source.version + " · "}
                    {source.locator}
                  </p>
                  <p>{source.scope}</p>
                  <blockquote>{source.excerpt}</blockquote>
                </section>
              ))}
            </>
          )}
        </Modal>
      )}
    </>
  );
}
