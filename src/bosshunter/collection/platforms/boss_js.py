"""JavaScript snippets shared by the CDP collector and the WebBridge sidecar.

Keeping them here means both collection paths parse BOSS pages with exactly
the same selectors. The snippets are plain strings so they can be shipped to
any browser runtime (CDP ``Runtime.evaluate`` or WebBridge ``evaluate``).
"""

from __future__ import annotations

JS_EXTRACT_LIST = """
(() => {
    const wraps = document.querySelectorAll('.job-card-wrap');
    const jobs = [];
    wraps.forEach((wrap) => {
        const box = wrap.querySelector('.job-card-box') || wrap;
        const nameEl = box.querySelector('.job-name');
        const salaryEl = box.querySelector('.job-salary');
        const tags = box.querySelectorAll('.tag-list li');
        const companyEl = box.querySelector('.boss-name') || box.querySelector('.company-name');
        const locationEl = box.querySelector('.company-location');
        const href = nameEl ? nameEl.getAttribute('href') : '';
        if (!nameEl || !href) return;
        jobs.push({
            title: nameEl.textContent.trim(), salary: salaryEl ? salaryEl.textContent.trim() : '',
            experience: tags[0] ? tags[0].textContent.trim() : '',
            education: tags[1] ? tags[1].textContent.trim() : '',
            company: companyEl ? companyEl.textContent.trim() : '',
            location: locationEl ? locationEl.textContent.trim() : '', url: href
        });
    });
    return JSON.stringify(jobs);
})()
"""

JS_EXTRACT_DETAIL = """
(() => {
    const info = {};
    info.title = document.querySelector('.info-primary .name h1')?.textContent?.trim()
        || document.querySelector('.name h1')?.textContent?.trim()
        || document.title.split('-')[0]?.trim();
    info.salary = document.querySelector('.info-primary .salary')?.textContent?.trim()
        || document.querySelector('.salary')?.textContent?.trim() || '';
    const tagItems = document.querySelectorAll('.info-primary .tag-list span');
    const tagTexts = Array.from(tagItems).map(t => t.textContent.trim());
    info.experience = tagTexts[0] || '';
    info.education = tagTexts[1] || '';
    const pageText = [info.title, info.experience, document.querySelector('.job-sec-text')?.textContent || ''].join(' ');
    info.recruitment_type = /校招|校园招聘|应届|毕业生|管培生/.test(pageText)
        ? 'campus'
        : (/社招|社会招聘/.test(pageText) ? 'experienced' : 'unknown');
    info.jd = document.querySelector('.job-sec-text')?.textContent?.trim() || '';
    const companyLinks = document.querySelectorAll('.sider-company .company-info a');
    info.company = '';
    for (const link of companyLinks) {
        const text = link.textContent.trim();
        if (text && !text.includes('http')) { info.company = text; break; }
    }
    if (!info.company) {
        const titleMatch = document.title.match(/_(.+?)招聘/);
        info.company = titleMatch ? titleMatch[1] : '';
    }
    const companyTags = document.querySelectorAll('.sider-company .res-industry-item, .company-info-item');
    info.company_size = ''; info.company_industry = '';
    companyTags.forEach(tag => {
        const text = tag.textContent.trim();
        if (text.includes('人')) info.company_size = text;
        else if (!info.company_industry) info.company_industry = text;
    });
    const bossSection = document.querySelector('.boss-info-attr') || document.querySelector('.job-boss-info');
    info.hr_name = bossSection?.querySelector('.name')?.textContent?.trim() || '';
    info.hr_title = bossSection?.querySelector('.title')?.textContent?.trim() || '';
    info.hr_active = document.querySelector('.boss-active-time')?.textContent?.trim() || '';
    info.url = window.location.pathname;
    return JSON.stringify(info);
})()
"""

JS_DETECT_COLLECTION_RISK = """
(() => {
    const text = (document.body?.innerText || '').slice(0, 10000);
    const url = String(location.href || '');
    const title = String(document.title || '');
    const hasExpectedContent = Boolean(document.querySelector(
        '.job-card-wrap, .job-sec-text, .job-detail, .job-primary'
    ));
    const captcha = document.querySelector(
        '.geetest_panel, .captcha, [class*="captcha"], [id*="captcha"], iframe[src*="captcha"], iframe[src*="verify"]'
    );
    if (captcha) return JSON.stringify({risk: 'captcha', evidence: 'captcha_element'});
    if (/captcha|security-check|\\/verify/i.test(url)) return JSON.stringify({risk: 'captcha', evidence: 'captcha_url'});
    if (/\\/web\\/user\\/(?:login|\\?ka=header-login)/i.test(url)) return JSON.stringify({risk: 'login_required', evidence: 'login_url'});
    // Search filters may legitimately contain 403 or other error words.
    // Only explicit error paths and status parameters indicate a blocked URL.
    const pageUrl = new URL(url);
    const blockedPath = /(?:^|\\/)(?:403|forbidden|access-denied)(?:\\/|$)/i.test(pageUrl.pathname);
    const blockedStatus = Array.from(pageUrl.searchParams).some(([key, value]) =>
        /^(?:code|status|error|error_code|status_code)$/i.test(key)
        && /^(?:403|forbidden|access-denied)$/i.test(value)
    );
    if (blockedPath || blockedStatus) {
        return JSON.stringify({risk: 'blocked', evidence: 'blocked_url'});
    }
    if (/^(?:403(?:\\s+forbidden)?|forbidden|access denied|访问被拒绝|账号异常|账号受限)/i.test(title.trim())) {
        return JSON.stringify({risk: 'blocked', evidence: 'blocked_title'});
    }
    if (!hasExpectedContent && /验证码|安全验证|完成验证/.test(text)) {
        return JSON.stringify({risk: 'captcha', evidence: 'captcha_page'});
    }
    if (!hasExpectedContent && /(?:^|\\n)\\s*403(?:\\s+forbidden)?\\s*(?:\\n|$)|访问被拒绝|账号异常|账号受限/i.test(text)) {
        return JSON.stringify({risk: 'blocked', evidence: 'blocked_page'});
    }
    if (!hasExpectedContent && /操作频繁|访问频繁|请求频繁|稍后再试|频率限制/.test(text)) {
        return JSON.stringify({risk: 'rate_limit', evidence: 'rate_limit_page'});
    }
    return JSON.stringify({risk: null});
})()
"""
