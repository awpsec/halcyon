import { Fragment } from "react";

const LINK_PATTERN =
  /([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|(?:https?:\/\/|www\.)[^\s<]+|(?<!@)(?:[a-z0-9-]+\.)+[a-z]{2,}(?:\/[^\s<]*)?)/gi;

function isEmail(raw: string) {
  return /^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$/i.test(raw);
}

function normalizeUrl(raw: string) {
  if (isEmail(raw)) {
    return `mailto:${raw}`;
  }
  return raw.startsWith("http://") || raw.startsWith("https://") ? raw : `https://${raw}`;
}

export function LinkifiedText({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  const lines = text.split("\n");

  return (
    <div className={className}>
      {lines.map((line, lineIndex) => {
        const parts = line.split(LINK_PATTERN);
        return (
          <Fragment key={`line-${lineIndex}`}>
            {parts.map((part, index) => {
              if (!part) return null;
              if (LINK_PATTERN.test(part)) {
                LINK_PATTERN.lastIndex = 0;
                return (
                  <a key={`part-${lineIndex}-${index}`} href={normalizeUrl(part)} target="_blank" rel="noreferrer">
                    {part}
                  </a>
                );
              }
              LINK_PATTERN.lastIndex = 0;
              return <Fragment key={`part-${lineIndex}-${index}`}>{part}</Fragment>;
            })}
            {lineIndex < lines.length - 1 ? <br /> : null}
          </Fragment>
        );
      })}
    </div>
  );
}
