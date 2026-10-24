"""Tech-stack detection from a page's raw HTML — free, no extra requests.

Patterns match distinctive script hosts, cookie names or generator tags, not
generic words, so false positives stay rare. Approach adapted from Harvey
(github.com/ethanplusai/harvey, MIT); see NOTICE.md.
"""

from __future__ import annotations

import re

FINGERPRINTS: dict[str, tuple[str, list[str]]] = {
    # name: (category, patterns)
    "WordPress": ("cms", [r"wp-content/", r"wp-includes/", r'name="generator" content="WordPress']),
    "Shopify": ("ecommerce", [r"cdn\.shopify\.com", r"myshopify\.com"]),
    "WooCommerce": ("ecommerce", [r"wp-content/plugins/woocommerce", r"wc-ajax="]),
    "BigCommerce": ("ecommerce", [r"cdn\d*\.bigcommerce\.com"]),
    "Magento": ("ecommerce", [r"Mage\.Cookies", r"static/version\d+/frontend"]),
    "Wix": ("cms", [r"static\.parastorage\.com", r"static\.wixstatic\.com"]),
    "Squarespace": ("cms", [r"static1\.squarespace\.com", r"<!-- This is Squarespace"]),
    "Webflow": ("cms", [r"assets\.website-files\.com", r"website-files\.com", r"data-wf-domain"]),
    "Framer": ("cms", [r"framerusercontent\.com", r"framer\.com/m/"]),
    "Drupal": ("cms", [r'name="generator" content="Drupal', r"sites/default/files"]),
    "Ghost": ("cms", [r'name="generator" content="Ghost']),
    "HubSpot CMS": ("cms", [r"hs-sites\.com", r"hubspotusercontent"]),
    "Next.js": ("framework", [r"/_next/static/", r"__NEXT_DATA__"]),
    "Nuxt": ("framework", [r"/_nuxt/", r"__NUXT__"]),
    "Gatsby": ("framework", [r'id="___gatsby"']),
    "Google Analytics": ("analytics", [r"googletagmanager\.com/gtag/js", r"google-analytics\.com/analytics\.js"]),
    "Google Tag Manager": ("analytics", [r"googletagmanager\.com/gtm\.js", r"GTM-[A-Z0-9]{4,}"]),
    "Segment": ("analytics", [r"cdn\.segment\.com/analytics\.js", r"cdn\.segment\.com"]),
    "Mixpanel": ("analytics", [r"cdn\.mxpnl\.com", r"mixpanel\.init"]),
    "Amplitude": ("analytics", [r"cdn\.amplitude\.com", r"amplitude\.getInstance"]),
    "Heap": ("analytics", [r"cdn\.heapanalytics\.com"]),
    "PostHog": ("analytics", [r"posthog\.com/static", r"posthog\.init"]),
    "Hotjar": ("analytics", [r"static\.hotjar\.com"]),
    "Microsoft Clarity": ("analytics", [r"clarity\.ms/tag"]),
    "Plausible": ("analytics", [r"plausible\.io/js"]),
    "Google Ads": ("advertising", [r"googleadservices\.com", r"AW-\d{6,}"]),
    "Meta Pixel": ("advertising", [r"connect\.facebook\.net/[^\"']*fbevents\.js", r"fbq\('init'"]),
    "LinkedIn Insight": ("advertising", [r"snap\.licdn\.com/li\.lms-analytics", r"_linkedin_partner_id"]),
    "TikTok Pixel": ("advertising", [r"analytics\.tiktok\.com"]),
    "HubSpot": ("marketing", [r"js\.hs-scripts\.com", r"js\.hsforms\.net", r"js\.hs-analytics\.net"]),
    "Marketo": ("marketing", [r"munchkin\.marketo\.net", r"mktoForms"]),
    "Pardot": ("marketing", [r"pi\.pardot\.com", r"go\.pardot\.com"]),
    "Mailchimp": ("marketing", [r"chimpstatic\.com", r"list-manage\.com"]),
    "Klaviyo": ("marketing", [r"static\.klaviyo\.com", r"klaviyo\.com/onsite"]),
    "ActiveCampaign": ("marketing", [r"trackcmp\.net"]),
    "Salesforce": ("crm", [r"\.my\.salesforce\.com", r"salesforce\.com/embeddedservice", r"service\.force\.com", r"salesforceliveagent"]),
    "6sense": ("intent", [r"j\.6sc\.co"]),
    "Clearbit": ("intent", [r"tag\.clearbitscripts\.com", r"x\.clearbitjs\.com"]),
    "RB2B": ("intent", [r"b2bjsstore", r"rb2b\.com/js"]),
    "Intercom": ("support", [r"widget\.intercom\.io", r"js\.intercomcdn\.com"]),
    "Drift": ("support", [r"js\.driftt\.com"]),
    "Zendesk": ("support", [r"static\.zdassets\.com", r"zendesk\.com/embeddable"]),
    "Crisp": ("support", [r"client\.crisp\.chat"]),
    "Tidio": ("support", [r"code\.tidio\.co"]),
    "LiveChat": ("support", [r"cdn\.livechatinc\.com"]),
    "Freshchat": ("support", [r"wchat\.freshchat\.com"]),
    "Qualified": ("support", [r"js\.qualified\.com"]),
    "Calendly": ("scheduling", [r"assets\.calendly\.com", r"calendly\.com/[\w\-]+"]),
    "Chili Piper": ("scheduling", [r"js\.chilipiper\.com"]),
    "Stripe": ("payments", [r"js\.stripe\.com"]),
    "Paddle": ("payments", [r"cdn\.paddle\.com"]),
    "Typeform": ("forms", [r"embed\.typeform\.com"]),
    "Cloudflare": ("infra", [r"/cdn-cgi/"]),
    "Vercel": ("infra", [r"/_vercel/insights", r"/_vercel/speed-insights"]),
    "Netlify": ("infra", [r"netlify-identity-widget"]),
    "Optimizely": ("experimentation", [r"cdn\.optimizely\.com"]),
    "VWO": ("experimentation", [r"dev\.visualwebsiteoptimizer\.com"]),
    "OneTrust": ("privacy", [r"cdn\.cookielaw\.org", r"otSDKStub"]),
    "Cookiebot": ("privacy", [r"consent\.cookiebot\.com"]),
}

_compiled = {
    name: (category, [re.compile(p, re.IGNORECASE) for p in patterns])
    for name, (category, patterns) in FINGERPRINTS.items()
}


def detect(html: str, max_bytes: int = 400_000) -> dict[str, list[str]]:
    """Return {category: [tool, ...]} for tools visible in the HTML."""
    sample = (html or "")[:max_bytes]
    found: dict[str, list[str]] = {}
    for name, (category, patterns) in _compiled.items():
        if any(p.search(sample) for p in patterns):
            found.setdefault(category, []).append(name)
    return found
