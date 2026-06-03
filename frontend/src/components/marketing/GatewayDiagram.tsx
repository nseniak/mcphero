export function GatewayDiagram() {
  const upstreams = [
    "Mixpanel",
    "Notion",
    "Stripe",
    "MongoDB",
    "GitHub",
    "Your own MCP",
  ];

  // Vertical layout sized for the hero's right column (~420px wide).
  const W = 380;
  const H = 360;

  const teammate = { x: 100, y: 8, w: 180, h: 54 };
  const hero = { x: 100, y: 90, w: 180, h: 80 };

  const chipW = 160;
  const chipH = 36;
  const chipColX = [10, 210] as const;
  const chipRowY = [220, 270, 320] as const;
  const chips = upstreams.map((name, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    return {
      name,
      x: chipColX[col],
      y: chipRowY[row],
      col,
    };
  });

  const sourceX = hero.x + hero.w / 2;
  const sourceY = hero.y + hero.h;

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label="Teammate's AI client connects to MCP Hero, which fans out to Mixpanel, Notion, Stripe, MongoDB, GitHub, and your own MCPs"
      className="w-full h-auto max-w-md mx-auto"
    >
      <defs>
        <marker
          id="arrow"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#71717a" />
        </marker>
      </defs>

      <g>
        <rect
          x={teammate.x}
          y={teammate.y}
          width={teammate.w}
          height={teammate.h}
          rx="10"
          fill="#ffffff"
          stroke="#d4d4d8"
          strokeWidth="1.5"
        />
        <text
          x={teammate.x + teammate.w / 2}
          y={teammate.y + 24}
          textAnchor="middle"
          fontFamily="ui-sans-serif, system-ui, sans-serif"
          fontSize="14"
          fontWeight="600"
          fill="#18181b"
        >
          Teammate
        </text>
        <text
          x={teammate.x + teammate.w / 2}
          y={teammate.y + 43}
          textAnchor="middle"
          fontFamily="ui-sans-serif, system-ui, sans-serif"
          fontSize="12"
          fill="#71717a"
        >
          Claude · Cursor · ChatGPT
        </text>
      </g>

      <line
        x1={teammate.x + teammate.w / 2}
        y1={teammate.y + teammate.h}
        x2={teammate.x + teammate.w / 2}
        y2={hero.y - 2}
        stroke="#71717a"
        strokeWidth="1.5"
        markerEnd="url(#arrow)"
      />

      <g>
        <rect
          x={hero.x}
          y={hero.y}
          width={hero.w}
          height={hero.h}
          rx="12"
          fill="#18181b"
          stroke="#18181b"
        />
        <g transform={`translate(${hero.x + 32}, ${hero.y + 12}) scale(0.075)`} aria-hidden="true">
          <path
            d="M200 20 L360 100 L340 300 L200 440 L60 300 L40 100 Z"
            fill="#2563EB"
            stroke="#FBBF24"
            strokeWidth="22"
            strokeLinejoin="round"
          />
          <g transform="translate(80, 95) scale(1.35)">
            <path
              d="M18 84.8528L85.8822 16.9706C95.2548 7.59798 110.451 7.59798 119.823 16.9706V16.9706C129.196 26.3431 129.196 41.5391 119.823 50.9117L68.5581 102.177"
              stroke="white"
              strokeWidth="14"
              strokeLinecap="round"
              fill="none"
            />
            <path
              d="M69.2652 101.47L119.823 50.9117C129.196 41.5391 144.392 41.5391 153.765 50.9117L154.118 51.2652C163.491 60.6378 163.491 75.8338 154.118 85.2063L92.7248 146.6C89.6006 149.724 89.6006 154.789 92.7248 157.913L105.331 170.52"
              stroke="white"
              strokeWidth="14"
              strokeLinecap="round"
              fill="none"
            />
            <path
              d="M102.853 33.9411L52.6482 84.1457C43.2756 93.5183 43.2756 108.714 52.6482 118.087V118.087C62.0208 127.459 77.2167 127.459 86.5893 118.087L136.794 67.8822"
              stroke="white"
              strokeWidth="14"
              strokeLinecap="round"
              fill="none"
            />
          </g>
        </g>
        <text
          x={hero.x + 70}
          y={hero.y + 35}
          textAnchor="start"
          fontFamily="ui-sans-serif, system-ui, sans-serif"
          fontSize="15"
          fontWeight="600"
          fill="#ffffff"
        >
          MCP Hero
        </text>
        <text
          x={hero.x + hero.w / 2}
          y={hero.y + 64}
          textAnchor="middle"
          fontFamily="ui-sans-serif, system-ui, sans-serif"
          fontSize="12"
          fill="#a1a1aa"
        >
          one MCP endpoint
        </text>
      </g>

      {chips.map(({ name, x, y, col }) => {
        const isLeftCol = col === 0;
        const targetX = isLeftCol ? x + chipW - 4 : x + 4;
        const targetY = y + chipH / 2;
        const midY = (sourceY + targetY) / 2;
        return (
          <path
            key={`arrow-${name}`}
            d={`M ${sourceX} ${sourceY} C ${sourceX} ${midY}, ${sourceX} ${targetY}, ${targetX} ${targetY}`}
            fill="none"
            stroke="#71717a"
            strokeWidth="1.5"
          />
        );
      })}
      {chips.map(({ name, x, y }) => (
        <g key={`chip-${name}`}>
          <rect
            x={x}
            y={y}
            width={chipW}
            height={chipH}
            rx="8"
            fill="#ffffff"
            stroke="#d4d4d8"
            strokeWidth="1.5"
          />
          <text
            x={x + chipW / 2}
            y={y + 22}
            textAnchor="middle"
            fontFamily="ui-sans-serif, system-ui, sans-serif"
            fontSize="13"
            fontWeight="500"
            fill="#18181b"
          >
            {name}
          </text>
        </g>
      ))}
    </svg>
  );
}
