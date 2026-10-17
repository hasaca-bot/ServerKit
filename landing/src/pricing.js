/* All prices, in USD, excluding VAT. Data only: pages render from this.
   Edit numbers here; the HTML only carries a copy as no-JS fallback. */
window.PRICING = {
  currency: "USD",
  yearlyMonthsFree: 2,   // yearly price = monthly price x (12 - this)

  plans: [
    {
      id: "hobby", name: "Hobby", monthly: 0,
      blurb: "For side projects and trying things out.",
      cta: "Start free",
      features: [
        "1 member",
        "3 services",
        "1 free database",
        "Apps sleep after 15 minutes idle",
        "Automatic TLS on your subdomain"
      ]
    },
    {
      id: "pro", name: "Pro", monthly: 19, featured: true,
      blurb: "For developers shipping real apps.",
      cta: "Start with Pro",
      features: [
        "Unlimited members",
        "25 services",
        "Preview environments per pull request",
        "Custom domains with automatic TLS",
        "5 MCP keys for AI assistants"
      ]
    },
    {
      id: "team", name: "Team", monthly: 59,
      blurb: "For teams that need accountability.",
      cta: "Start with Team",
      features: [
        "Everything in Pro",
        "Audit log",
        "30-day log retention",
        "Priority support",
        "SSO (planned)"
      ]
    }
  ],

  compute: [
    { name: "Free",     cpu: "0.1 vCPU", ram: "256 MB", monthly: 0,  note: "Sleeps after 15 min idle" },
    { name: "Starter",  cpu: "0.5 vCPU", ram: "512 MB", monthly: 5,  note: "Always on" },
    { name: "Standard", cpu: "1 vCPU",   ram: "2 GB",   monthly: 12, note: "Always on" },
    { name: "Pro",      cpu: "2 vCPU",   ram: "4 GB",   monthly: 24, note: "Always on" }
  ],

  databases: [
    { name: "Free",     storage: "256 MB", monthly: 0,  backups: "No backups" },
    { name: "Basic",    storage: "1 GB",   monthly: 6,  backups: "Daily backups" },
    { name: "Standard", storage: "10 GB",  monthly: 18, backups: "Daily backups" }
  ],

  vps: [
    { name: "VPS-1", cpu: "2 vCPU", ram: "2 GB", disk: "40 GB",  monthly: 9 },
    { name: "VPS-2", cpu: "2 vCPU", ram: "4 GB", disk: "80 GB",  monthly: 17 },
    { name: "VPS-4", cpu: "4 vCPU", ram: "8 GB", disk: "160 GB", monthly: 33 }
  ],
  vpsIncludes: "Every VPS includes the MCP endpoint and weekly snapshots.",

  /* Feature matrix. Values: true (included), false (not included) or a string. */
  matrixPlans: ["Hobby", "Pro", "Team"],
  matrix: [
    { group: "Limits", rows: [
      { label: "Members",                 values: ["1", "Unlimited", "Unlimited"] },
      { label: "Services",                values: ["3", "25", "25"] },
      { label: "Free database",           values: ["1", "1", "1"] },
      { label: "Sleep when idle",         values: ["After 15 min", "Free compute only", "Free compute only"] }
    ]},
    { group: "Deploys", rows: [
      { label: "GitHub auto-deploy",      values: [true, true, true] },
      { label: "Zero-downtime deploys, health checks, rollback", values: [true, true, true] },
      { label: "Preview environments per pull request", values: [false, true, true] },
      { label: "Custom domains with automatic TLS",     values: [false, true, true] }
    ]},
    { group: "AI and automation", rows: [
      { label: "Built-in MCP endpoint",   values: [true, true, true] },
      { label: "MCP keys",                values: ["1", "5", "5"] },
      { label: "Infrastructure-as-code manifest", values: [true, true, true] }
    ]},
    { group: "Teams and support", rows: [
      { label: "Roles",                   values: [false, true, true] },
      { label: "Audit log",               values: [false, false, true] },
      { label: "30-day log retention",    values: [false, false, true] },
      { label: "Priority support",        values: [false, false, true] },
      { label: "SSO",                     values: [false, false, "Planned"] }
    ]}
  ]
};
