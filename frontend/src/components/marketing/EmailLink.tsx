import { useEffect, useState } from "react";

interface EmailLinkProps {
  email: string;
  className?: string;
  onClick?: () => void;
}

// JS-decoded mailto: the static HTML never contains an "@" between user and
// domain, no "mailto:" prefix, and no "[at]" / "(at)" word — none of the
// patterns dumb harvesters match on. The real address gets assembled and
// the link wired up only after we confirm we're in a real browser
// (`__PRERENDER_INJECTED` is set during the build-time puppeteer prerender,
// so we skip reveal in that pass and the obfuscated form is what ships).
export function EmailLink({ email, className, onClick }: EmailLinkProps) {
  const [revealed, setRevealed] = useState(false);
  const [user, domain] = email.split("@");

  useEffect(() => {
    const w = window as unknown as { __PRERENDER_INJECTED?: boolean };
    if (w.__PRERENDER_INJECTED === true) return;
    setRevealed(true);
  }, []);

  if (!revealed) {
    return (
      <span className={className}>
        {user}
        <span aria-hidden="true"> · </span>
        {domain}
      </span>
    );
  }

  return (
    <a href={`mailto:${email}`} onClick={onClick} className={className}>
      {email}
    </a>
  );
}
