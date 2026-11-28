import { memo, type ReactNode } from "react";
import Markdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const markdownComponents: Components = {
  table: ({ children }) => (
    <div className="analysis-markdown-table" tabIndex={0} role="region" aria-label="数据表格">
      <table>{children}</table>
    </div>
  ),
  pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
};

export const AnalysisMarkdown = memo(function AnalysisMarkdown({
  source,
  revealing = false,
  embedded = false,
  citationLinks,
}: {
  source: string;
  revealing?: boolean;
  embedded?: boolean;
  citationLinks?: ReadonlyMap<string, ReactNode>;
}) {
  return (
    <div className="analysis-markdown" data-revealing={revealing}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={
          citationLinks
            ? {
                ...markdownComponents,
                a: ({ href, children, ...props }) =>
                  citationLinks.has(href ?? "") ? (
                    citationLinks.get(href ?? "")
                  ) : (
                    <a href={href} {...props}>
                      {children}
                    </a>
                  ),
              }
            : markdownComponents
        }
        disallowedElements={embedded ? ["img", "a"] : undefined}
        unwrapDisallowed
        skipHtml
        urlTransform={(url) => defaultUrlTransform(url) || undefined}
      >
        {source}
      </Markdown>
    </div>
  );
});
