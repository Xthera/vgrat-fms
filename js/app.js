```javascript
/* 
 * VGrat FMS Dashboard
 *
 * Architecture:
 *
 *   PRULink / fund scraper
 *          ↓
 *      data.json
 *          ↓
 *       app.js
 *
 *   LIVE MARKET INDICES
 *          ↓
 *   Twelve Data API
 *          ↓
 *       app.js
 *
 * IMPORTANT:
 * Market indices are intentionally NOT loaded from data.json.
 * The fund scraper and market-index system are completely independent.
 */


/* ============================================================
   LIVE INDEX CONFIGURATION
   ============================================================ */

/*
 * IMPORTANT:
 * Replace this with your Twelve Data API key.
 *
 * DO NOT commit a real API key to a public GitHub repository.
 * For a public production dashboard, use a serverless/backend
 * proxy instead.
 */
const TWELVE_DATA_API_KEY = 'YOUR_TWELVE_DATA_API_KEY';


/*
 * Twelve Data index symbols.
 *
 * If one of these symbols is unavailable on your particular
 * Twelve Data plan, the API will return an error for that symbol
 * and the remaining symbols will continue to load.
 *
 * The display names are controlled locally so your dashboard
 * remains consistent.
 */
const LIVE_INDEX_CONFIG = [
    {
        symbol: 'STI',
        name: 'STI',
        region: 'Singapore'
    },
    {
        symbol: 'SPX',
        name: 'S&P 500',
        region: 'United States'
    },
    {
        symbol: 'IXIC',
        name: 'NASDAQ',
        region: 'United States'
    },
    {
        symbol: 'DJI',
        name: 'Dow Jones',
        region: 'United States'
    },
    {
        symbol: 'N225',
        name: 'Nikkei 225',
        region: 'Japan'
    },
    {
        symbol: 'HSI',
        name: 'Hang Seng',
        region: 'Hong Kong'
    },
    {
        symbol: '000001',
        name: 'Shanghai Composite',
        region: 'China'
    },
    {
        symbol: 'FTSE',
        name: 'FTSE 100',
        region: 'United Kingdom'
    },
    {
        symbol: 'DAX',
        name: 'DAX',
        region: 'Germany'
    },
    {
        symbol: 'CAC',
        name: 'CAC 40',
        region: 'France'
    }
];


/*
 * Refresh interval for live indices.
 *
 * Twelve Data documents its index API as being updated every
 * minute, so 60 seconds is a sensible dashboard refresh rate.
 */
const LIVE_INDEX_REFRESH_MS = 60 * 1000;


/*
 * The market data remains available when an API request fails.
 * This stores the last successful result.
 */
let liveIndexLastUpdated = null;
let liveIndexLastAttempt = null;
let liveIndexStatus = 'not-started';
let liveIndexTimer = null;
let liveIndexRequestInProgress = false;


/* ============================================================
   GLOBAL DASHBOARD STATE
   ============================================================ */

let allFunds = [];
let fundHistory = {};
let fundHistoryFull = {};

let marketIndices = [];
let marketNews = [];
let riskFactors = [];
let sortedNews = [];

let dashboardData = null;

let currentView = 'overview';
let activeCategory = 'all';
let activeRisk = 'all';

let sortBy = 'name';
let sortDir = 'asc';
let searchQuery = '';

let perfChart = null;
let modalChart = null;

let currentPeriod = '1Y';

let selectedFundNames = [];
let hiddenFunds = new Set();


/* ============================================================
   CHART DEFAULTS
   ============================================================ */

function getChartTheme() {
    const isLight =
        document.documentElement.getAttribute('data-theme') === 'light';

    return {
        text: isLight ? '#475569' : '#94a3b8',
        grid: isLight ? '#e2e8f0' : '#1e293b',
    };
}

Chart.defaults.color = getChartTheme().text;
Chart.defaults.borderColor = getChartTheme().grid;
Chart.defaults.font.family = "'Inter', sans-serif";


/* ============================================================
   INIT
   ============================================================ */

document.addEventListener('DOMContentLoaded', () => {

    initNavigation();
    initTheme();
    initRefresh();
    initSorting();
    initModal();
    initNewsModal();
    initMoversControls();
    initDailyAutoRefresh();

    /*
     * Start the normal dashboard data load.
     *
     * IMPORTANT:
     * loadData() loads funds/news/risk/etc.
     * It does NOT load market indices anymore.
     */
    loadData();

    /*
     * Start independent live-index system.
     */
    initLiveIndices();
});


/* ============================================================
   NAVIGATION
   ============================================================ */

function initNavigation() {

    document.querySelectorAll('.nav-item').forEach(item => {

        item.addEventListener('click', (e) => {

            e.preventDefault();

            const view = item.dataset.view;

            switchView(view);
        });
    });
}


function switchView(view) {

    currentView = view;

    document.querySelectorAll('.nav-item').forEach(i => {

        i.classList.toggle(
            'active',
            i.dataset.view === view
        );
    });

    document.querySelectorAll('.view').forEach(v => {

        v.classList.toggle(
            'active',
            v.id === `view-${view}`
        );
    });

    const titles = {

        overview: {
            title: 'Dashboard Overview',
            subtitle:
                'Monitoring all VGrat fund movements with global market context'
        },

        funds: {
            title: 'All Funds',
            subtitle:
                'Complete VGrat fund universe with prices and performance'
        },

        news: {
            title: 'Global Affairs',
            subtitle:
                'Market news, indices, and risk factors impacting fund performance'
        },

        risk: {
            title: 'Risk Monitor',
            subtitle:
                'Key risk factors and fund risk distribution'
        },

    };

    const t = titles[view] || titles.overview;

    document.getElementById('pageTitle').textContent = t.title;

    document.getElementById('pageSubtitle').textContent = t.subtitle;
}


/* ============================================================
   THEME
   ============================================================ */

function initTheme() {

    const container = document.getElementById('themeToggle');

    if (!container) return;

    container.addEventListener('click', (e) => {

        const btn = e.target.closest('.theme-btn');

        if (!btn || btn.classList.contains('active')) return;

        const value = btn.dataset.themeValue;

        document.documentElement.setAttribute(
            'data-theme',
            value
        );

        container.querySelectorAll('.theme-btn').forEach(b => {

            const active = b === btn;

            b.classList.toggle('active', active);

            b.setAttribute(
                'aria-selected',
                active ? 'true' : 'false'
            );
        });


        const theme = getChartTheme();

        Chart.defaults.color = theme.text;
        Chart.defaults.borderColor = theme.grid;


        if (perfChart) {
            renderPerfChart();
        }


        if (modalChart) {

            const fundNameEl =
                document.getElementById('modalFundName');

            const fund = fundNameEl
                ? allFunds.find(
                    f => f.name === fundNameEl.textContent
                )
                : null;

            if (fund) {
                renderModalChart(
                    fundHistory[fund.name] || []
                );
            }
        }
    });
}


/* ============================================================
   REFRESH
   ============================================================ */

function initRefresh() {

    const btn = document.getElementById('refreshBtn');

    if (!btn) return;

    btn.addEventListener('click', async () => {

        btn.innerHTML = 'Loading...';

        /*
         * Refresh both systems independently.
         *
         * Fund data → data.json
         * Index data → Twelve Data
         */
        await Promise.allSettled([
            loadData(),
            loadLiveIndices(true)
        ]);

        btn.innerHTML =
            getRefreshIcon() + ' Refresh';
    });
}


/* ============================================================
   DAILY FUND AUTO REFRESH
   ============================================================ */

/*
 * Re-pulls data.json once per day after 8am local time.
 *
 * This remains the fund-data refresh mechanism.
 *
 * Live indices are handled separately by the 60-second
 * live-index timer.
 */

function initDailyAutoRefresh() {

    const STORAGE_KEY =
        'vgratLastAutoRefreshDate';


    const mark = () => {

        localStorage.setItem(
            STORAGE_KEY,
            new Date().toDateString()
        );
    };


    mark();


    setInterval(() => {

        const now = new Date();

        const todayKey =
            now.toDateString();


        if (
            now.getHours() >= 8 &&
            localStorage.getItem(STORAGE_KEY) !== todayKey
        ) {

            mark();

            /*
             * Only refresh the fund/data.json system here.
             *
             * Live indices already refresh independently.
             */
            loadData();
        }

    }, 15 * 60 * 1000);
}


/* ============================================================
   LIVE MARKET INDICES
   ============================================================ */

/*
 * Initialise the independent market-index system.
 */
function initLiveIndices() {

    /*
     * First request immediately.
     */
    loadLiveIndices();


    /*
     * Then refresh independently every minute.
     */
    if (liveIndexTimer) {
        clearInterval(liveIndexTimer);
    }


    liveIndexTimer = setInterval(() => {

        loadLiveIndices();

    }, LIVE_INDEX_REFRESH_MS);
}


/*
 * Build the Twelve Data batch quote URL.
 *
 * Twelve Data supports comma-separated symbols for batch
 * requests to the same endpoint.
 */
function buildLiveIndexUrl() {

    const symbols =
        LIVE_INDEX_CONFIG
            .map(item => item.symbol)
            .join(',');


    const params = new URLSearchParams({

        symbol: symbols,

        apikey: TWELVE_DATA_API_KEY,

        /*
         * Singapore timezone makes timestamps easier to
         * interpret inside this dashboard.
         */
        timezone: 'Asia/Singapore',

        /*
         * Request a compact response.
         */
        dp: '5'
    });


    return `https://api.twelvedata.com/quote?${params.toString()}`;
}


/*
 * Convert API number safely.
 */
function toNumber(value) {

    const n = Number(value);

    return Number.isFinite(n)
        ? n
        : null;
}


/*
 * Extract a single Twelve Data quote into our dashboard's
 * existing marketIndices format.
 */
function normalizeLiveIndex(config, quote) {

    if (!quote) return null;


    /*
     * Twelve Data can return:
     *
     * {
     *   status: "error",
     *   message: "..."
     * }
     *
     * for an individual symbol.
     */
    if (quote.status === 'error') {

        console.warn(
            `Live index unavailable: ${config.symbol}`,
            quote.message || ''
        );

        return null;
    }


    const value =
        toNumber(
            quote.close ??
            quote.price ??
            quote.last
        );


    const changePct =
        toNumber(
            quote.percent_change ??
            quote.percentChange ??
            0
        );


    /*
     * If there is no usable price, don't replace an existing
     * working value with broken data.
     */
    if (value === null) {
        return null;
    }


    return {

        /*
         * Keep the dashboard's existing structure.
         */
        name: config.name,

        symbol: config.symbol,

        value: value,

        change_pct:
            changePct !== null
                ? changePct
                : 0,

        region: config.region,

        currency:
            quote.currency || '',

        is_market_open:
            quote.is_market_open === true,

        datetime:
            quote.datetime || '',

        timestamp:
            quote.timestamp || null,

        source:
            'Twelve Data',

        live:
            true
    };
}


/*
 * Main live-index loader.
 *
 * This function NEVER touches:
 *
 *   allFunds
 *   fundHistory
 *   marketNews
 *   riskFactors
 *   dashboardData
 *
 * Therefore an index failure cannot break the fund system.
 */
async function loadLiveIndices(force = false) {

    /*
     * Don't start another request while one is already running.
     */
    if (liveIndexRequestInProgress) {
        return;
    }


    /*
     * API key check.
     */
    if (
        !TWELVE_DATA_API_KEY ||
        TWELVE_DATA_API_KEY === 'YOUR_TWELVE_DATA_API_KEY'
    ) {

        liveIndexStatus = 'no-api-key';

        console.warn(
            'VGrat: Twelve Data API key has not been configured.'
        );

        renderLiveIndexStatus();

        /*
         * Do NOT clear marketIndices.
         *
         * If data.json contains old index data, we deliberately
         * leave it alone so the dashboard doesn't suddenly go blank.
         */
        return;
    }


    liveIndexRequestInProgress = true;

    liveIndexLastAttempt = new Date();


    try {

        const url = buildLiveIndexUrl();


        const controller =
            new AbortController();


        /*
         * Avoid hanging forever if the API becomes unavailable.
         */
        const timeout =
            setTimeout(
                () => controller.abort(),
                15000
            );


        const response =
            await fetch(
                url,
                {
                    method: 'GET',
                    cache: 'no-store',
                    signal: controller.signal
                }
            );


        clearTimeout(timeout);


        if (!response.ok) {

            throw new Error(
                `Twelve Data HTTP ${response.status}`
            );
        }


        const payload =
            await response.json();


        /*
         * Twelve Data can return a top-level error.
         */
        if (payload.status === 'error') {

            throw new Error(
                payload.message ||
                'Twelve Data returned an error'
            );
        }


        /*
         * Batch response normally comes back as:
         *
         * {
         *   "STI": {...},
         *   "SPX": {...},
         *   ...
         * }
         *
         * We also support a single quote response just in case
         * the API returns one.
         */
        const nextIndices = [];


        LIVE_INDEX_CONFIG.forEach(config => {

            let quote =
                payload[config.symbol];


            /*
             * Some API configurations may return a direct
             * quote object rather than a symbol-keyed object.
             */
            if (
                !quote &&
                LIVE_INDEX_CONFIG.length === 1
            ) {
                quote = payload;
            }


            const normalized =
                normalizeLiveIndex(
                    config,
                    quote
                );


            if (normalized) {
                nextIndices.push(normalized);
            }
        });


        /*
         * We only replace the existing marketIndices if we
         * actually received valid index data.
         */
        if (nextIndices.length > 0) {

            marketIndices =
                nextIndices;


            liveIndexLastUpdated =
                new Date();

            liveIndexStatus =
                'live';


            /*
             * Immediately redraw only the index UI.
             */
            renderIndicesTicker();

            renderIndicesList();


            /*
             * Optional status indicator.
             */
            renderLiveIndexStatus();


            console.log(
                `VGrat: ${nextIndices.length} live indices updated.`
            );

        } else {

            throw new Error(
                'Twelve Data returned no usable index quotes.'
            );
        }

    } catch (error) {

        liveIndexStatus = 'error';


        console.error(
            'VGrat live index update failed:',
            error
        );


        /*
         * IMPORTANT:
         *
         * Do NOT empty marketIndices.
         *
         * The last successful values remain displayed.
         */
        renderLiveIndexStatus();

    } finally {

        liveIndexRequestInProgress = false;
    }
}


/*
 * Render a small status indicator if your HTML has one.
 *
 * If there is no #indexLiveStatus element, this function
 * simply does nothing.
 *
 * This means you don't have to change index HTML immediately.
 */
function renderLiveIndexStatus() {

    const el =
        document.getElementById('indexLiveStatus');


    if (!el) return;


    if (liveIndexStatus === 'live') {

        const time =
            liveIndexLastUpdated
                ? liveIndexLastUpdated.toLocaleTimeString()
                : '--';


        el.textContent =
            `LIVE • Updated ${time}`;

        el.classList.remove(
            'offline',
            'error'
        );

        el.classList.add('live');

        return;
    }


    if (liveIndexStatus === 'no-api-key') {

        el.textContent =
            'LIVE DATA • API KEY NOT CONFIGURED';

        el.classList.remove('live');

        el.classList.add('error');

        return;
    }


    if (liveIndexStatus === 'error') {

        const time =
            liveIndexLastUpdated
                ? liveIndexLastUpdated.toLocaleTimeString()
                : 'No successful update';


        el.textContent =
            `STALE • Last update ${time}`;

        el.classList.remove('live');

        el.classList.add('error');

        return;
    }


    el.textContent =
        'Market data loading...';
}


/*
 * Public helper.
 *
 * You can manually call:
 *
 * refreshLiveIndices()
 *
 * from the browser console or another UI button.
 */
function refreshLiveIndices() {

    return loadLiveIndices(true);
}


/* ============================================================
   SORTING
   ============================================================ */

function initSorting() {

    document
        .querySelectorAll('.fund-table th[data-sort]')
        .forEach(th => {

            th.addEventListener('click', () => {

                const sort =
                    th.dataset.sort;


                if (sortBy === sort) {

                    sortDir =
                        sortDir === 'asc'
                            ? 'desc'
                            : 'asc';

                } else {

                    sortBy = sort;

                    sortDir = 'asc';
                }


                renderFundTable();
            });
        });
}


/* ============================================================
   FUND MODAL
   ============================================================ */

function initModal() {

    const close =
        document.getElementById('modalClose');

    if (close) {
        close.addEventListener(
            'click',
            closeFundModal
        );
    }


    const modal =
        document.getElementById('fundModal');

    if (modal) {

        modal.addEventListener('click', (e) => {

            if (e.target.id === 'fundModal') {
                closeFundModal();
            }

        });
    }
}


function openFundModal(fundName) {

    const fund =
        allFunds.find(
            f => f.name === fundName
        );


    if (!fund) return;


    document.getElementById(
        'modalFundName'
    ).textContent = fund.name;


    document.getElementById(
        'modalFundMeta'
    ).textContent =
        `${fund.category} | ${fund.currency} | Inception: ${fund.effective_date}`;


    const history =
        fundHistory[fund.name] || [];


    const returns =
        calculateFundReturns(history);


    const bid =
        toNumber(fund.bid);


    const statsHtml = `

        <div class="modal-stat">

            <div class="modal-stat-label">
                Bid Price
            </div>

            <div class="modal-stat-value">
                ${bid !== null ? bid.toFixed(5) : '--'}
            </div>

        </div>


        <div class="modal-stat">

            <div class="modal-stat-label">
                1D Return
            </div>

            <div class="modal-stat-value ${returns['1D'] >= 0 ? 'gain' : 'loss'}">
                ${returns['1D'] != null
                    ? (returns['1D'] >= 0 ? '+' : '') + returns['1D'] + '%'
                    : '--'}
            </div>

        </div>


        <div class="modal-stat">

            <div class="modal-stat-label">
                1M Return
            </div>

            <div class="modal-stat-value ${returns['1M'] >= 0 ? 'gain' : 'loss'}">
                ${returns['1M'] != null
                    ? (returns['1M'] >= 0 ? '+' : '') + returns['1M'] + '%'
                    : '--'}
            </div>

        </div>


        <div class="modal-stat">

            <div class="modal-stat-label">
                3M Return
            </div>

            <div class="modal-stat-value ${returns['3M'] >= 0 ? 'gain' : 'loss'}">
                ${returns['3M'] != null
                    ? (returns['3M'] >= 0 ? '+' : '') + returns['3M'] + '%'
                    : '--'}
            </div>

        </div>

    `;


    document.getElementById(
        'modalStats'
    ).innerHTML = statsHtml;


    const holdings =
        fund.holdings || [];


    const holdingsHtml =
        holdings.length
            ? `

        <h4 class="modal-section-title">
            Top 10 Holdings
        </h4>

        <div class="holdings-list">

            ${holdings.map((h, i) => {

                const weight =
                    toNumber(h.weight);

                return `

                    <div class="holding-item">

                        <span class="holding-rank">
                            ${i + 1}
                        </span>

                        <span class="holding-name">
                            ${h.name}
                        </span>

                        <span class="holding-weight">
                            ${weight !== null
                                ? weight.toFixed(1)
                                : '--'}%
                        </span>

                    </div>

                `;

            }).join('')}

        </div>

    `
            : '';


    document.getElementById(
        'modalHoldings'
    ).innerHTML = holdingsHtml;


    renderModalChart(history);


    document.getElementById(
        'fundModal'
    ).classList.add('active');
}


function closeFundModal() {

    document.getElementById(
        'fundModal'
    ).classList.remove('active');


    if (modalChart) {

        modalChart.destroy();

        modalChart = null;
    }
}


/* ============================================================
   NEWS MODAL
   ============================================================ */

function initNewsModal() {

    const closeBtn =
        document.getElementById(
            'newsModalClose'
        );


    if (closeBtn) {

        closeBtn.addEventListener(
            'click',
            closeNewsModal
        );
    }


    const overlay =
        document.getElementById(
            'newsModal'
        );


    if (overlay) {

        overlay.addEventListener('click', (e) => {

            if (e.target.id === 'newsModal') {
                closeNewsModal();
            }

        });
    }
}


function openNewsModal(index) {

    const n =
        sortedNews[index];


    if (!n) return;


    const hasRealLink =
        !!n.url;


    const sourceLine =
        n.source
            ? `Source: ${n.source}`
            : 'Source: VGrat Market Intelligence Wire';


    document.getElementById(
        'modalNewsTitle'
    ).textContent = n.title;


    document.getElementById(
        'modalNewsMeta'
    ).textContent = n.date;


    document.getElementById(
        'modalNewsBody'
    ).innerHTML = `

        <div class="news-modal-tags">

            <span class="timeline-category">
                ${n.category}
            </span>

            <span class="timeline-impact ${n.impact}">
                ${n.impact} Impact
            </span>

        </div>


        ${
            hasRealLink

                ? `<a
                    class="news-modal-readmore"
                    href="${n.url}"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Read full article on ${n.source} &rarr;
                  </a>`

                : `<div class="news-modal-report">
                    <p>${n.title}</p>
                  </div>`
        }


        <div class="news-modal-source">
            ${sourceLine}
        </div>

    `;


    document.getElementById(
        'newsModal'
    ).classList.add('active');
}


function closeNewsModal() {

    document.getElementById(
        'newsModal'
    ).classList.remove('active');
}


/* ============================================================
   FUND RETURNS
   ============================================================ */

function calculateFundReturns(history) {

    const returns = {};


    if (history.length < 2) {
        return returns;
    }


    const current =
        toNumber(
            history[history.length - 1].bid
        );


    if (current === null) {
        return returns;
    }


    for (
        const [period, daysBack]
        of [
            ['1D', 1],
            ['1W', 5],
            ['1M', 20],
            ['3M', 60]
        ]
    ) {

        const idx =
            Math.max(
                0,
                history.length - 1 - daysBack
            );


        const past =
            toNumber(
                history[idx]?.bid
            );


        if (past === null || past <= 0) {
            returns[period] = null;
            continue;
        }


        returns[period] =
            round(
                ((current - past) / past) * 100,
                2
            );
    }


    return returns;
}


function round(val, decimals) {

    const factor =
        Math.pow(10, decimals);

    return Math.round(
        val * factor
    ) / factor;
}


/* ============================================================
   FUND DATA LOADING
   ============================================================ */

/*
 * IMPORTANT:
 *
 * This function ONLY loads the static/scheduled dashboard data.
 *
 * It deliberately does NOT do:
 *
 *     marketIndices = dashboardData.indices || [];
 *
 * because indices are now handled by Twelve Data separately.
 */

async function loadData() {

    try {

        const res =
            await fetch(
                './data.json?t=' + Date.now()
            );


        if (!res.ok) {

            throw new Error(
                `data.json HTTP ${res.status}`
            );
        }


        dashboardData =
            await res.json();


        /*
         * FUND DATA
         */
        allFunds =
            dashboardData.funds?.funds || [];


        fundHistory =
            dashboardData.history || {};


        fundHistoryFull =
            dashboardData.history_full || {};


        /*
         * IMPORTANT:
         *
         * No marketIndices assignment here.
         */
        marketNews =
            dashboardData.news || [];


        riskFactors =
            dashboardData.riskFactors || [];


        const updated =
            dashboardData.funds?.updated_at ||
            dashboardData.funds?.updated_on ||
            '';


        const lastUpdated =
            document.getElementById(
                'lastUpdated'
            );


        if (lastUpdated) {

            lastUpdated.textContent =
                `Updated ${updated}`;
        }


        /*
         * Render everything except the independent
         * live-index system.
         */
        renderAll();


        /*
         * Restore chart state after funds are available.
         */
        loadChartState();


        initChartControls();


        renderSelectedFunds();


        renderPerfChart();


        const refreshBtn =
            document.getElementById(
                'refreshBtn'
            );


        if (refreshBtn) {

            refreshBtn.innerHTML =
                getRefreshIcon() +
                ' Refresh';
        }


    } catch (e) {

        console.error(
            'Failed to load fund/dashboard data:',
            e
        );


        const refreshBtn =
            document.getElementById(
                'refreshBtn'
            );


        if (refreshBtn) {

            refreshBtn.innerHTML =
                getRefreshIcon() +
                ' Retry';
        }
    }
}


function getRefreshIcon() {

    return `
        <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
        >
            <polyline points="23 4 23 10 17 10"/>
            <polyline points="1 20 1 14 7 14"/>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/>
            <path d="M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
        </svg>
    `;
}


/* ============================================================
   RENDER ALL
   ============================================================ */

function renderAll() {

    sortedNews =
        [...marketNews].sort(
            (a, b) =>
                String(b.date || '')
                    .localeCompare(
                        String(a.date || '')
                    )
        );


    renderTopMovers();

    renderNews();

    /*
     * Render current live index state.
     *
     * This does NOT fetch anything.
     *
     * The actual fetching is handled by loadLiveIndices().
     */
    renderIndicesTicker();

    renderFundTable();

    renderCategoryFilters();

    renderRiskFilters();

    renderPerfChart();

    renderFullNewsView();

    renderRiskFactors();

    renderCommodities();

    renderCurrencies();

    renderBonds();

    renderFullMarkets();

    renderLiveIndexStatus();
}


/* ============================================================
   KPIs
   ============================================================ */

function truncateName(name, maxLen) {

    return name.length > maxLen
        ? name.substring(0, maxLen - 2) + '...'
        : name;
}


function calculateReturns(period) {

    return allFunds.map(fund => {

        const history =
            fundHistory[fund.name] || [];


        if (history.length < 2) {

            return {
                name: fund.name,
                return: 0
            };
        }


        const current =
            toNumber(
                history[history.length - 1].bid
            );


        if (current === null) {

            return {
                name: fund.name,
                return: 0
            };
        }


        const daysBack = {
            '1D': 1,
            '1W': 5,
            '1M': 20,
            '3M': 60
        }[period] || 60;


        const idx =
            Math.max(
                0,
                history.length - 1 - daysBack
            );


        const past =
            toNumber(
                history[idx]?.bid
            );


        if (
            past === null ||
            past <= 0
        ) {

            return {
                name: fund.name,
                return: 0,
                current,
                past
            };
        }


        const ret =
            ((current - past) / past) * 100;


        return {
            name: fund.name,
            return: ret,
            current,
            past
        };

    });
}


/* ============================================================
   TOP MOVERS
   ============================================================ */

let moversPeriod = 'D';


function getMoversReturns(period) {

    if (period === 'Y') {

        return allFunds.map(fund => ({

            name: fund.name,

            return:
                calculatePeriodReturn(
                    fund.name,
                    '1Y'
                )

        }));
    }


    const periodMap = {
        D: '1D',
        W: '1W',
        M: '1M'
    };


    return calculateReturns(
        periodMap[period] || '1D'
    );
}


function moverFundLabel(name) {

    const fund =
        allFunds.find(
            f => f.name === name
        );


    const code =
        fund &&
        fund.codeVerified !== false &&
        fund.code
            ? fund.code
            : '';


    return code
        ? `[${code}] ${name}`
        : name;
}


function renderTopMovers() {

    const returns =
        getMoversReturns(
            moversPeriod
        ).sort(
            (a, b) =>
                b.return - a.return
        );


    const gainers =
        returns.slice(0, 10);


    const losers =
        returns.slice(-10).reverse();


    const gainersList =
        document.getElementById(
            'gainersList'
        );


    if (gainersList) {

        gainersList.innerHTML =
            gainers.map(g => `

                <div
                    class="mover-item"
                    onclick="openFundModal('${escapeName(g.name)}')"
                >

                    <span class="mover-name">
                        ${moverFundLabel(g.name)}
                    </span>

                    <span class="mover-pct up">
                        +${g.return.toFixed(2)}%
                    </span>

                </div>

            `).join('');
    }


    const losersList =
        document.getElementById(
            'losersList'
        );


    if (losersList) {

        losersList.innerHTML =
            losers.map(l => `

                <div
                    class="mover-item"
                    onclick="openFundModal('${escapeName(l.name)}')"
                >

                    <span class="mover-name">
                        ${moverFundLabel(l.name)}
                    </span>

                    <span class="mover-pct down">
                        ${l.return.toFixed(2)}%
                    </span>

                </div>

            `).join('');
    }
}


function initMoversControls() {

    const container =
        document.getElementById(
            'moversPeriod'
        );


    if (!container) return;


    container.addEventListener('click', (e) => {

        const btn =
            e.target.closest(
                '.period-btn'
            );


        if (
            !btn ||
            btn.classList.contains('active')
        ) {
            return;
        }


        moversPeriod =
            btn.dataset.period;


        container
            .querySelectorAll('.period-btn')
            .forEach(b => {

                const active =
                    b === btn;


                b.classList.toggle(
                    'active',
                    active
                );


                b.setAttribute(
                    'aria-selected',
                    active
                        ? 'true'
                        : 'false'
                );
            });


        renderTopMovers();
    });
}


function escapeName(name) {

    return String(name || '')
        .replace(
            /'/g,
            "\\'"
        )
        .replace(
            /"/g,
            '&quot;'
        );
}


/* ============================================================
   NEWS
   ============================================================ */

function renderNews() {

    const el =
        document.getElementById(
            'newsList'
        );


    if (!el) return;


    el.innerHTML =
        sortedNews
            .slice(0, 8)
            .map((n, index) => `

                <div
                    class="news-item"
                    onclick="openNewsModal(${index})"
                >

                    <div class="news-date">
                        ${n.date || ''}
                    </div>

                    <div class="news-content">

                        <div class="news-title">
                            ${n.title || ''}
                        </div>

                        <span class="news-category">
                            ${n.category || ''}
                        </span>

                        <span
                            class="news-impact ${n.impact || ''}"
                            style="margin-left:8px"
                        >
                            ${n.impact || ''}
                        </span>

                    </div>

                </div>

            `)
            .join('');
}


/* ============================================================
   INDICES TICKER
   ============================================================ */

/*
 * This function ONLY renders marketIndices.
 *
 * It does not care where marketIndices came from.
 *
 * Currently:
 *
 *     Twelve Data → marketIndices → this function
 */
function renderIndicesTicker() {

    const tickerTrack =
        document.getElementById(
            'tickerTrack'
        );


    if (!tickerTrack) return;


    if (!marketIndices.length) {

        tickerTrack.innerHTML = `
            <span class="ticker-loading">
                Loading market indices...
            </span>
        `;

        return;
    }


    const items =
        marketIndices.map(idx => {

            const value =
                toNumber(idx.value);


            const change =
                toNumber(idx.change_pct) ?? 0;


            const cls =
                change >= 0
                    ? 'up'
                    : 'down';


            const arrow =
                change >= 0
                    ? '&#9650;'
                    : '&#9660;';


            return `

                <div class="ticker-item">

                    <span class="ticker-symbol">
                        ${idx.name}
                    </span>

                    <span class="ticker-value">
                        ${
                            value !== null
                                ? value.toLocaleString(
                                    undefined,
                                    {
                                        minimumFractionDigits: 2,
                                        maximumFractionDigits: 5
                                    }
                                )
                                : '--'
                        }
                    </span>

                    <span class="ticker-change ${cls}">
                        ${arrow}
                        ${Math.abs(change).toFixed(2)}%
                    </span>

                </div>

            `;

        }).join('');


    /*
     * Duplicate for seamless ticker scrolling.
     */
    tickerTrack.innerHTML =
        items + items;
}


/*
 * Separate renderer for the Global Affairs index list.
 *
 * This is deliberately separate from renderFullNewsView()
 * so the live index system can update it every minute without
 * having to rebuild the entire news timeline.
 */
function renderIndicesList() {

    const el =
        document.getElementById(
            'indicesList'
        );


    if (!el) return;


    if (!marketIndices.length) {

        el.innerHTML = `
            <div class="index-item">
                <span class="index-name">
                    Loading market indices...
                </span>
            </div>
        `;

        return;
    }


    el.innerHTML =
        marketIndices.map(idx => {

            const value =
                toNumber(idx.value);


            const change =
                toNumber(idx.change_pct) ?? 0;


            const cls =
                change >= 0
                    ? 'up'
                    : 'down';


            const arrow =
                change >= 0
                    ? '\u25b2'
                    : '\u25bc';


            return `

                <div class="index-item">

                    <span class="index-name">
                        ${idx.name}
                    </span>

                    <span class="index-value">
                        ${
                            value !== null
                                ? value.toLocaleString(
                                    undefined,
                                    {
                                        minimumFractionDigits: 2,
                                        maximumFractionDigits: 5
                                    }
                                )
                                : '--'
                        }
                    </span>

                    <span class="index-change ${cls}">
                        ${arrow}
                        ${Math.abs(change).toFixed(2)}%
                    </span>

                </div>

            `;

        }).join('');
}


/* ============================================================
   FUND TABLE
   ============================================================ */

/*
 * Only funds successfully pulled from the live Prudential
 * scrape are shown on the All Funds page.
 */
function getVerifiedFunds() {

    return allFunds.filter(
        f =>
            f &&
            f.dataSource === 'verified-live'
    );
}


function renderFundTable() {

    const returnMap = {};


    allFunds.forEach(f => {

        returnMap[f.name] =
            calculatePeriodReturn(
                f.name,
                '6M'
            );
    });


    let funds =
        getVerifiedFunds().map(f => ({

            ...f,

            change:
                returnMap[f.name] || 0

        }));


    if (activeCategory !== 'all') {

        funds =
            funds.filter(
                f =>
                    f.category === activeCategory
            );
    }


    if (activeRisk !== 'all') {

        funds =
            funds.filter(
                f =>
                    f.riskCategory === activeRisk
            );
    }


    if (searchQuery) {

        funds =
            funds.filter(
                f =>
                    String(f.name || '')
                        .toLowerCase()
                        .includes(searchQuery)
            );
    }


    funds.sort((a, b) => {

        let va = a[sortBy];
        let vb = b[sortBy];


        if (sortBy === 'change') {

            va = a.change;
            vb = b.change;
        }


        if (sortBy === 'risk') {

            va = a.riskCategory;
            vb = b.riskCategory;
        }


        if (typeof va === 'string') {

            return sortDir === 'asc'
                ? va.localeCompare(vb || '')
                : (vb || '').localeCompare(va);
        }


        va = Number(va) || 0;
        vb = Number(vb) || 0;


        return sortDir === 'asc'
            ? va - vb
            : vb - va;
    });


    const tbody =
        document.getElementById(
            'fundTableBody'
        );


    const tbody2 =
        document.getElementById(
            'fundTableBody2'
        );


    const html =
        funds.map(f => {

            const bid =
                toNumber(f.bid);


            const offer =
                toNumber(f.offer);


            const change =
                Number(f.change) || 0;


            const changeCls =
                change > 0.01
                    ? 'up'
                    : change < -0.01
                        ? 'down'
                        : 'flat';


            const arrow =
                change > 0.01
                    ? '&#9650;'
                    : change < -0.01
                        ? '&#9660;'
                        : '&#9644;';


            const riskClsTier =
                riskTier(
                    f.riskCategory
                );


            const riskCls =
                riskClsTier >= 0
                    ? RISK_TIER_CLASSES[riskClsTier]
                    : '';


            return `

                <tr
                    onclick="openFundModal('${escapeName(f.name)}')"
                >

                    <td>

                        <span class="fund-name-cell">
                            ${f.name || ''}
                        </span>

                        <span class="fund-code-cell">
                            ${
                                f.codeVerified === false
                                    ? ''
                                    : (f.code || '')
                            }
                        </span>

                    </td>


                    <td>
                        <span class="category-badge">
                            ${f.category || ''}
                        </span>
                    </td>


                    <td>
                        <span class="risk-badge ${riskCls}">
                            ${f.riskCategory || ''}
                        </span>
                    </td>


                    <td class="num">
                        ${
                            bid !== null
                                ? bid.toFixed(5)
                                : '--'
                        }
                    </td>


                    <td class="num">
                        ${
                            offer !== null
                                ? offer.toFixed(5)
                                : '--'
                        }
                    </td>


                    <td>
                        ${f.currency || ''}
                    </td>


                    <td class="num">

                        <span
                            class="change-badge ${changeCls}"
                        >

                            ${arrow}

                            ${
                                change >= 0
                                    ? '+'
                                    : ''
                            }

                            ${change.toFixed(2)}%

                        </span>

                    </td>


                    <td>
                        ${f.effective_date || ''}
                    </td>

                </tr>

            `;

        }).join('');


    if (tbody) {
        tbody.innerHTML = html;
    }


    if (tbody2) {
        tbody2.innerHTML = html;
    }
}


/* ============================================================
   CATEGORY FILTERS
   ============================================================ */

function renderCategoryFilters() {

    const verifiedFunds =
        getVerifiedFunds();


    const categories =
        [...new Set(
            verifiedFunds.map(
                f => f.category
            )
        )].sort();


    const chips =
        ['all', ...categories]
            .map(cat => {

                const count =
                    cat === 'all'
                        ? verifiedFunds.length
                        : verifiedFunds.filter(
                            f =>
                                f.category === cat
                        ).length;


                const label =
                    cat === 'all'
                        ? 'All'
                        : cat;


                return `

                    <span
                        class="chip ${
                            activeCategory === cat
                                ? 'active'
                                : ''
                        }"
                        onclick="filterCategory('${cat}')"
                    >

                        ${label} (${count})

                    </span>

                `;

            }).join('');


    const cf1 =
        document.getElementById(
            'categoryFilters'
        );


    if (cf1) {
        cf1.innerHTML = chips;
    }


    const cf2 =
        document.getElementById(
            'categoryFilters2'
        );


    if (cf2) {
        cf2.innerHTML = chips;
    }
}


function filterCategory(cat) {

    activeCategory = cat;

    renderFundTable();

    renderCategoryFilters();
}


/* ============================================================
   RISK FILTERS
   ============================================================ */

function riskTier(riskLabel) {

    const r =
        (riskLabel || '')
            .toLowerCase();


    if (r.includes('higher')) {
        return 3;
    }


    if (
        r.includes('medium') &&
        r.includes('high')
    ) {
        return 2;
    }


    if (r.includes('medium')) {
        return 1;
    }


    if (r.includes('low')) {
        return 0;
    }


    return -1;
}


const RISK_TIER_CLASSES = [
    'risk-tier-0',
    'risk-tier-1',
    'risk-tier-2',
    'risk-tier-3'
];


function renderRiskFilters() {

    const verifiedFunds =
        getVerifiedFunds();


    const risks =
        [...new Set(
            verifiedFunds.map(
                f => f.riskCategory
            )
        )]
        .sort(
            (a, b) =>
                riskTier(a) -
                riskTier(b)
        );


    const chips =
        ['all', ...risks]
            .map(risk => {

                const count =
                    risk === 'all'
                        ? verifiedFunds.length
                        : verifiedFunds.filter(
                            f =>
                                f.riskCategory === risk
                        ).length;


                const label =
                    risk === 'all'
                        ? 'All'
                        : risk;


                return `

                    <span
                        class="chip ${
                            activeRisk === risk
                                ? 'active'
                                : ''
                        }"
                        onclick="filterRisk('${risk}')"
                    >

                        ${label} (${count})

                    </span>

                `;

            }).join('');


    const rf =
        document.getElementById(
            'riskFilters'
        );


    if (rf) {
        rf.innerHTML = chips;
    }
}


function filterRisk(risk) {

    activeRisk = risk;

    renderFundTable();

    renderRiskFilters();
}


/* ============================================================
   PERFORMANCE CHART
   ============================================================ */

const CHART_COLORS = [
    '#3b82f6',
    '#22c55e',
    '#f59e0b',
    '#ef4444',
    '#8b5cf6',
    '#06b6d4',
    '#ec4899',
    '#84cc16',
    '#f97316',
    '#6366f1',
    '#14b8a6',
    '#eab308'
];


const PERIOD_WEEKS = {

    '1M': 4,

    '3M': 13,

    '6M': 26,

    '1Y': 52,

    '3Y': 156,

    '5Y': 260,

    '10Y': 520

};


function getPeriodData(fundName, period) {

    const hist =
        fundHistoryFull[fundName] || [];


    if (hist.length < 2) {

        return {
            dates: [],
            values: []
        };
    }


    const weeks =
        PERIOD_WEEKS[period] || 52;


    const startIdx =
        Math.max(
            0,
            hist.length - weeks
        );


    const slice =
        hist.slice(startIdx);


    if (slice.length < 2) {

        return {
            dates: [],
            values: []
        };
    }


    const basePrice =
        toNumber(slice[0].bid);


    if (
        basePrice === null ||
        basePrice <= 0
    ) {

        return {
            dates: [],
            values: []
        };
    }


    const dates =
        slice.map(
            h => h.date
        );


    const values =
        slice.map(h => {

            const bid =
                toNumber(h.bid);


            return bid !== null
                ? (bid / basePrice) * 100
                : 100;
        });


    return {
        dates,
        values
    };
}


function calculatePeriodReturn(
    fundName,
    period
) {

    const { values } =
        getPeriodData(
            fundName,
            period
        );


    if (values.length < 2) {
        return 0;
    }


    return (
        values[values.length - 1] -
        100
    );
}


function setActivePeriodButton(period) {

    const container =
        document.getElementById(
            'perfPeriod'
        );


    if (!container) return;


    container
        .querySelectorAll('.period-btn')
        .forEach(btn => {

            const isActive =
                btn.dataset.period === period;


            btn.classList.toggle(
                'active',
                isActive
            );


            btn.setAttribute(
                'aria-selected',
                isActive
                    ? 'true'
                    : 'false'
            );
        });
}


function initChartControls() {

    const periodSelector =
        document.getElementById(
            'perfPeriod'
        );


    if (periodSelector) {

        periodSelector.addEventListener(
            'click',
            (e) => {

                const btn =
                    e.target.closest(
                        '.period-btn'
                    );


                if (
                    !btn ||
                    btn.classList.contains('active')
                ) {
                    return;
                }


                currentPeriod =
                    btn.dataset.period;


                setActivePeriodButton(
                    currentPeriod
                );


                saveChartState();


                renderSelectedFunds();

                renderPerfChart();
            }
        );
    }


    const searchInput =
        document.getElementById(
            'chartFundSearch'
        );


    const dropdown =
        document.getElementById(
            'autocompleteDropdown'
        );


    let highlightIdx = -1;

    let currentMatches = [];


    if (
        searchInput &&
        dropdown
    ) {

        searchInput.addEventListener(
            'input',
            () => {

                const query =
                    searchInput.value
                        .toLowerCase()
                        .trim();


                if (!query) {

                    dropdown.classList.remove(
                        'show'
                    );

                    return;
                }


                currentMatches =
                    allFunds
                        .filter(f =>
                            String(f.name || '')
                                .toLowerCase()
                                .includes(query) ||

                            (
                                f.code &&
                                String(f.code)
                                    .toLowerCase()
                                    .includes(query)
                            )
                        )
                        .slice(0, 15);


                highlightIdx = -1;


                if (
                    currentMatches.length === 0
                ) {

                    dropdown.innerHTML =
                        '<div class="autocomplete-no-results">No funds found</div>';

                    dropdown.classList.add(
                        'show'
                    );

                    return;
                }


                dropdown.innerHTML =
                    currentMatches.map(
                        (f, i) => {

                            const alreadySel =
                                selectedFundNames
                                    .includes(
                                        f.name
                                    );


                            const ret =
                                calculatePeriodReturn(
                                    f.name,
                                    currentPeriod
                                );


                            const retStr =
                                (
                                    ret >= 0
                                        ? '+'
                                        : ''
                                ) +
                                ret.toFixed(2) +
                                '%';


                            const retColor =
                                ret >= 0
                                    ? '#22c55e'
                                    : '#ef4444';


                            return `

                                <div
                                    class="autocomplete-item ${
                                        alreadySel
                                            ? 'already-selected'
                                            : ''
                                    }"
                                    data-idx="${i}"
                                >

                                    <div>

                                        <div class="autocomplete-name">
                                            ${f.name}
                                        </div>

                                        <div class="autocomplete-cat">
                                            ${f.category || ''}
                                            ${
                                                f.codeVerified !== false &&
                                                f.code
                                                    ? ' &middot; ' + f.code
                                                    : ''
                                            }
                                        </div>

                                    </div>


                                    <span
                                        style="
                                            color:${retColor};
                                            font-size:12px;
                                            font-weight:600
                                        "
                                    >
                                        ${retStr}
                                    </span>

                                </div>

                            `;
                        }
                    ).join('');


                dropdown.classList.add(
                    'show'
                );
            }
        );


        dropdown.addEventListener(
            'click',
            (e) => {

                const item =
                    e.target.closest(
                        '.autocomplete-item'
                    );


                if (
                    !item ||
                    item.classList.contains(
                        'already-selected'
                    )
                ) {
                    return;
                }


                const idx =
                    parseInt(
                        item.dataset.idx,
                        10
                    );


                const fund =
                    currentMatches[idx];


                if (fund) {

                    addFundToChart(
                        fund.name
                    );
                }
            }
        );


        searchInput.addEventListener(
            'keydown',
            (e) => {

                if (
                    !dropdown.classList.contains(
                        'show'
                    )
                ) {
                    return;
                }


                if (e.key === 'ArrowDown') {

                    e.preventDefault();


                    highlightIdx =
                        Math.min(
                            highlightIdx + 1,
                            currentMatches.length - 1
                        );


                    updateHighlight();

                } else if (
                    e.key === 'ArrowUp'
                ) {

                    e.preventDefault();


                    highlightIdx =
                        Math.max(
                            highlightIdx - 1,
                            0
                        );


                    updateHighlight();

                } else if (
                    e.key === 'Enter' &&
                    highlightIdx >= 0
                ) {

                    e.preventDefault();


                    const fund =
                        currentMatches[
                            highlightIdx
                        ];


                    if (fund) {

                        addFundToChart(
                            fund.name
                        );
                    }

                } else if (
                    e.key === 'Escape'
                ) {

                    dropdown.classList.remove(
                        'show'
                    );

                    searchInput.blur();
                }
            }
        );


        function updateHighlight() {

            dropdown
                .querySelectorAll(
                    '.autocomplete-item'
                )
                .forEach((el, i) => {

                    el.classList.toggle(
                        'highlighted',
                        i === highlightIdx
                    );


                    if (
                        i === highlightIdx
                    ) {

                        el.scrollIntoView({
                            block: 'nearest',
                            behavior: 'smooth'
                        });
                    }
                });
        }


        document.addEventListener(
            'click',
            (e) => {

                if (
                    !e.target.closest(
                        '.autocomplete-wrapper'
                    )
                ) {

                    dropdown.classList.remove(
                        'show'
                    );
                }
            }
        );


        searchInput.addEventListener(
            'focus',
            () => {

                if (
                    searchInput.value.trim()
                ) {

                    searchInput.dispatchEvent(
                        new Event('input')
                    );
                }
            }
        );
    }
}


function addFundToChart(name) {

    if (
        selectedFundNames.includes(name)
    ) {
        return;
    }


    if (
        selectedFundNames.length >= 12
    ) {

        selectedFundNames.shift();
    }


    selectedFundNames.push(name);


    hiddenFunds.delete(name);


    saveChartState();


    const searchInput =
        document.getElementById(
            'chartFundSearch'
        );


    const dropdown =
        document.getElementById(
            'autocompleteDropdown'
        );


    if (searchInput) {
        searchInput.value = '';
    }


    if (dropdown) {
        dropdown.classList.remove(
            'show'
        );
    }


    renderSelectedFunds();

    renderPerfChart();
}


function saveChartState() {

    try {

        const state = {

            funds:
                selectedFundNames,

            period:
                currentPeriod,

            hidden:
                [...hiddenFunds]

        };


        window.name =
            'prulink_chart=' +
            JSON.stringify(state);

    } catch (e) {}
}


function loadChartState() {

    try {

        const raw =
            window.name;


        if (
            !raw ||
            !raw.startsWith(
                'prulink_chart='
            )
        ) {
            return;
        }


        const state =
            JSON.parse(
                raw.substring(
                    'prulink_chart='.length
                )
            );


        if (state.funds) {

            selectedFundNames =
                state.funds.filter(
                    n =>
                        allFunds.some(
                            f =>
                                f.name === n
                        )
                );
        }


        if (state.period) {

            currentPeriod =
                state.period;


            setActivePeriodButton(
                currentPeriod
            );
        }


        if (state.hidden) {

            hiddenFunds =
                new Set(
                    state.hidden.filter(
                        n =>
                            selectedFundNames
                                .includes(n)
                    )
                );
        }

    } catch (e) {}
}


function setDefaultFunds() {

    selectedFundNames = [];

    hiddenFunds.clear();
}


function renderSelectedFunds() {

    const container =
        document.getElementById(
            'selectedFunds'
        );


    if (!container) return;


    container.innerHTML =
        selectedFundNames.map(
            (name, i) => {

                const color =
                    CHART_COLORS[
                        i % CHART_COLORS.length
                    ];


                const isHidden =
                    hiddenFunds.has(name);


                const ret =
                    calculatePeriodReturn(
                        name,
                        currentPeriod
                    );


                const retStr =
                    (
                        ret >= 0
                            ? '+'
                            : ''
                    ) +
                    ret.toFixed(2) +
                    '%';


                const retColor =
                    ret >= 0
                        ? '#22c55e'
                        : '#ef4444';


                return `

                    <span
                        class="fund-chip ${
                            isHidden
                                ? 'hidden-line'
                                : ''
                        }"
                        style="
                            background:${color}20;
                            color:${color};
                            border:1px solid ${color}40
                        "
                        onclick="toggleFundLine('${escapeName(name)}')"
                    >

                        ${truncateName(name, 25)}

                        <span
                            style="
                                color:${retColor};
                                font-size:11px
                            "
                        >
                            ${retStr}
                        </span>

                        <span
                            class="chip-remove"
                            onclick="
                                event.stopPropagation();
                                removeFundLine('${escapeName(name)}')
                            "
                        >
                            \u2715
                        </span>

                    </span>

                `;
            }
        ).join('');
}


function toggleFundLine(name) {

    if (hiddenFunds.has(name)) {

        hiddenFunds.delete(name);

    } else {

        hiddenFunds.add(name);
    }


    saveChartState();

    renderSelectedFunds();

    renderPerfChart();
}


function removeFundLine(name) {

    selectedFundNames =
        selectedFundNames.filter(
            n => n !== name
        );


    hiddenFunds.delete(name);


    saveChartState();

    renderSelectedFunds();

    renderPerfChart();
}


function renderPerfChart() {

    const container =
        document.querySelector(
            '.chart-container'
        );


    if (!container) return;


    if (
        selectedFundNames.length === 0
    ) {

        if (perfChart) {

            perfChart.destroy();

            perfChart = null;
        }


        container.innerHTML = `
            <div class="chart-empty-state">

                <div class="chart-empty-icon">
                    \u25F2
                </div>

                <div class="chart-empty-text">
                    Search and add funds above to start comparing performance
                </div>

            </div>
        `;

        return;
    }


    const visibleFunds =
        selectedFundNames.filter(
            n =>
                !hiddenFunds.has(n)
        );


    if (
        visibleFunds.length === 0
    ) {

        if (perfChart) {

            perfChart.destroy();

            perfChart = null;
        }


        container.innerHTML = `
            <div class="chart-empty-state">

                <div class="chart-empty-icon">
                    \u25F2
                </div>

                <div class="chart-empty-text">
                    All funds are hidden. Click a fund tag to show it again.
                </div>

            </div>
        `;

        return;
    }


    let canvas =
        document.getElementById(
            'perfChart'
        );


    if (!canvas) {

        container.innerHTML =
            '<canvas id="perfChart"></canvas>';


        canvas =
            document.getElementById(
                'perfChart'
            );
    }


    let bestDates = [];


    visibleFunds.forEach(name => {

        const { dates } =
            getPeriodData(
                name,
                currentPeriod
            );


        if (
            dates.length >
            bestDates.length
        ) {

            bestDates = dates;
        }
    });


    const datasets =
        selectedFundNames.map(
            (name, i) => {

                const color =
                    CHART_COLORS[
                        i % CHART_COLORS.length
                    ];


                const isHidden =
                    hiddenFunds.has(name);


                const { values } =
                    getPeriodData(
                        name,
                        currentPeriod
                    );


                return {

                    label:
                        truncateName(
                            name,
                            35
                        ),

                    data:
                        values,

                    borderColor:
                        color,

                    backgroundColor:
                        color + '15',

                    borderWidth:
                        isHidden
                            ? 0
                            : 2,

                    pointRadius: 0,

                    pointHoverRadius: 4,

                    tension: 0.3,

                    fill: false,

                    hidden:
                        isHidden

                };
            }
        );


    if (perfChart) {
        perfChart.destroy();
    }


    const chartCanvas =
        document.getElementById(
            'perfChart'
        );


    if (!chartCanvas) return;


    perfChart =
        new Chart(
            chartCanvas,
            {
                type: 'line',

                data: {

                    labels:
                        bestDates,

                    datasets:
                        datasets

                },

                options: {

                    responsive: true,

                    maintainAspectRatio: false,

                    interaction: {
                        mode: 'index',
                        intersect: false
                    },

                    plugins: {

                        legend: {

                            display: true,

                            position: 'bottom',

                            labels: {

                                font: {
                                    size: 10
                                },

                                boxWidth: 12,

                                boxHeight: 12,

                                padding: 8
                            }
                        },


                        tooltip: {

                            callbacks: {

                                label: (ctx) => {

                                    const val =
                                        ctx.parsed.y;


                                    const ret =
                                        val - 100;


                                    return `${ctx.dataset.label}: ${
                                        ret >= 0
                                            ? '+'
                                            : ''
                                    }${ret.toFixed(2)}%`;
                                }
                            }
                        }
                    },


                    scales: {

                        x: {

                            grid: {
                                color:
                                    getChartTheme().grid
                            },


                            ticks: {

                                font: {
                                    size: 10
                                },


                                maxTicksLimit: 12,


                                callback:
                                    function(
                                        val,
                                        idx
                                    ) {

                                        const label =
                                            this.getLabelForValue(
                                                val
                                            );


                                        if (!label) {
                                            return '';
                                        }


                                        const d =
                                            new Date(
                                                label +
                                                'T00:00:00'
                                            );


                                        return d.toLocaleDateString(
                                            'en-US',
                                            {
                                                month:
                                                    'short',
                                                year:
                                                    '2-digit'
                                            }
                                        );
                                    }
                            }
                        },


                        y: {

                            grid: {
                                color:
                                    getChartTheme().grid
                            },


                            ticks: {

                                callback:
                                    (v) =>
                                        (
                                            v - 100
                                        ).toFixed(0) +
                                        '%'
                            },


                            title: {

                                display: true,

                                text:
                                    'Return (%)',

                                font: {
                                    size: 11
                                }
                            }
                        }
                    }
                }
            }
        );
}


/* ============================================================
   MODAL CHART
   ============================================================ */

function renderModalChart(history) {

    if (!history || history.length === 0) {
        return;
    }


    const labels =
        history.map(
            h => h.date
        );


    const data =
        history.map(
            h =>
                toNumber(h.bid)
        );


    if (modalChart) {
        modalChart.destroy();
    }


    const ctx =
        document.getElementById(
            'modalChart'
        );


    if (!ctx) return;


    modalChart =
        new Chart(
            ctx,
            {
                type: 'line',

                data: {

                    labels,

                    datasets: [{

                        label:
                            'Bid Price',

                        data,

                        borderColor:
                            '#3b82f6',

                        backgroundColor:
                            'rgba(59, 130, 246, 0.1)',

                        fill: true,

                        tension: 0.3,

                        pointRadius: 0,

                        borderWidth: 2,

                    }]
                },


                options: {

                    responsive: true,

                    maintainAspectRatio: false,


                    plugins: {

                        legend: {
                            display: false
                        }
                    },


                    scales: {

                        x: {

                            grid: {
                                display: false
                            },


                            ticks: {

                                maxTicksLimit: 8,

                                font: {
                                    size: 10
                                }
                            }
                        },


                        y: {

                            grid: {

                                color:
                                    getChartTheme().grid
                            },


                            ticks: {

                                font: {
                                    size: 11
                                }
                            }
                        }
                    }
                }
            }
        );
}


/* ============================================================
   FULL NEWS VIEW
   ============================================================ */

function renderFullNewsView() {

    let html = '';

    let lastMonth = '';


    sortedNews.forEach(
        (n, index) => {

            const dateString =
                String(
                    n.date || ''
                );


            const monthKey =
                dateString.substring(
                    0,
                    7
                );


            const monthDate =
                new Date(
                    dateString +
                    'T00:00:00'
                );


            const monthName =
                isNaN(
                    monthDate.getTime()
                )
                    ? monthKey
                    : monthDate.toLocaleString(
                        'en-US',
                        {
                            month:
                                'long',
                            year:
                                'numeric'
                        }
                    );


            if (
                monthKey !== lastMonth
            ) {

                html += `
                    <div class="timeline-month-header">
                        ${monthName}
                    </div>
                `;

                lastMonth = monthKey;
            }


            html += `

                <div
                    class="timeline-item"
                    onclick="openNewsModal(${index})"
                >

                    <div class="timeline-date">
                        ${n.date || ''}
                    </div>

                    <div class="timeline-content">

                        <div class="timeline-title">
                            ${n.title || ''}
                        </div>

                        <span class="timeline-category">
                            ${n.category || ''}
                        </span>

                        <span class="timeline-impact ${n.impact || ''}">
                            ${n.impact || ''} Impact
                        </span>

                    </div>

                </div>

            `;
        }
    );


    const newsEl =
        document.getElementById(
            'fullNewsList'
        );


    if (newsEl) {
        newsEl.innerHTML = html;
    }


    /*
     * Do NOT rebuild indices manually here anymore.
     *
     * The independent live-index renderer handles it.
     */
    renderIndicesList();
}


/* ============================================================
   RISK FACTORS
   ============================================================ */

const RISK_FACTOR_CATEGORY_MAP = {

    'US Fed Policy': [
        'US Equity',
        'Global Equity',
        'Fixed Income',
        'Dividend',
        'Multi-Asset'
    ],

    'ECB Policy': [
        'European Equity',
        'Global Equity',
        'Fixed Income'
    ],

    'Geopolitical Tensions': [
        'Global Equity',
        'Asian Equity',
        'China Equity',
        'India Equity',
        'Sector',
        'ESG',
        'Multi-Asset'
    ],

    'Oil Prices': [
        'Sector',
        'Global Equity',
        'Multi-Asset',
        'Real Estate'
    ],

    'China Growth': [
        'China Equity',
        'Asian Equity',
        'Global Equity'
    ],

    'SGD/USD': [
        'Singapore Equity',
        'Cash',
        'Fixed Income'
    ],

    'Inflation Risk': [
        'Fixed Income',
        'Dividend',
        'Real Estate',
        'Multi-Asset',
        'Cash'
    ],

    'Regional Flows': [
        'Singapore Equity',
        'Asian Equity',
        'China Equity'
    ],

};


const RISK_LEVEL_RANK = {

    high: 3,

    medium: 2,

    low: 1

};


function getFundsAffectedByFactor(
    factorName
) {

    const categories =
        RISK_FACTOR_CATEGORY_MAP[
            factorName
        ] || [];


    return allFunds.filter(
        f =>
            categories.includes(
                f.category
            )
    );
}


function renderRiskFactors() {

    const sorted =
        [...riskFactors].sort(
            (a, b) =>
                (
                    RISK_LEVEL_RANK[
                        b.level
                    ] || 0
                ) -
                (
                    RISK_LEVEL_RANK[
                        a.level
                    ] || 0
                )
        );


    const html =
        sorted.map(
            (f, index) => {

                const affectedFunds =
                    getFundsAffectedByFactor(
                        f.factor
                    );


                const categories =
                    RISK_FACTOR_CATEGORY_MAP[
                        f.factor
                    ] || [];


                const fundChips =
                    affectedFunds
                        .map(
                            fund => `

                                <span
                                    class="risk-fund-chip"
                                    onclick="
                                        event.stopPropagation();
                                        openFundModal('${escapeName(fund.name)}')
                                    "
                                >
                                    ${fund.name}
                                </span>

                            `
                        )
                        .join('');


                return `

                    <div
                        class="risk-item-block"
                        id="riskBlock-${index}"
                    >

                        <div
                            class="risk-item-header"
                            onclick="toggleRiskFactor(${index})"
                        >

                            <div>

                                <div class="risk-factor-name">
                                    ${f.factor}
                                </div>

                                <div class="risk-factor-status">
                                    ${f.status}
                                </div>

                            </div>


                            <div class="risk-item-header-right">

                                <span class="risk-level ${f.level}">
                                    ${f.level}
                                </span>

                                <span class="risk-toggle-hint">

                                    Affects
                                    ${affectedFunds.length}
                                    fund${
                                        affectedFunds.length === 1
                                            ? ''
                                            : 's'
                                    }

                                    <span class="risk-toggle-chevron">
                                        &#9662;
                                    </span>

                                </span>

                            </div>

                        </div>


                        <div class="risk-affected">

                            <div class="risk-affected-label">

                                Affects
                                ${affectedFunds.length}
                                fund${
                                    affectedFunds.length === 1
                                        ? ''
                                        : 's'
                                }

                                across
                                ${categories.length}
                                categor${
                                    categories.length === 1
                                        ? 'y'
                                        : 'ies'
                                }

                                ${
                                    categories.length
                                        ? `(${categories.join(', ')})`
                                        : ''
                                }

                            </div>


                            <div class="risk-fund-chips">

                                ${
                                    fundChips ||
                                    '<span class="risk-affected-label">No matching funds</span>'
                                }

                            </div>

                        </div>

                    </div>

                `;
            }
        ).join('');


    const riskMonitorEl =
        document.getElementById(
            'riskMonitorList'
        );


    if (riskMonitorEl) {

        riskMonitorEl.innerHTML =
            html;
    }
}


function toggleRiskFactor(index) {

    const block =
        document.getElementById(
            `riskBlock-${index}`
        );


    if (block) {

        block.classList.toggle(
            'expanded'
        );
    }
}


/* ============================================================
   COMMODITIES
   ============================================================ */

function renderCommodities() {

    const commodities =
        dashboardData
            ? (
                dashboardData.commodities ||
                []
            )
            : [];


    const el =
        document.getElementById(
            'commoditiesList'
        );


    if (!el) return;


    el.innerHTML =
        commodities.map(c => {

            const value =
                toNumber(c.value) ?? 0;


            const change =
                toNumber(c.change_pct) ?? 0;


            const cls =
                change >= 0
                    ? 'up'
                    : 'down';


            const arrow =
                change >= 0
                    ? '\u25b2'
                    : '\u25bc';


            return `

                <div class="market-item">

                    <div>

                        <div class="market-name">
                            ${c.name || ''}
                        </div>

                        <div
                            style="
                                font-size:10px;
                                color:var(--text-muted)
                            "
                        >
                            ${c.unit || ''}
                        </div>

                    </div>


                    <div style="text-align:right">

                        <div class="market-value">
                            ${value.toLocaleString(
                                undefined,
                                {
                                    minimumFractionDigits: 2
                                }
                            )}
                        </div>

                        <div class="market-change ${cls}">
                            ${arrow}
                            ${Math.abs(change).toFixed(2)}%
                        </div>

                    </div>

                </div>

            `;

        }).join('');
}


/* ============================================================
   CURRENCIES
   ============================================================ */

function renderCurrencies() {

    const currencies =
        dashboardData
            ? (
                dashboardData.currencies ||
                []
            )
            : [];


    const el =
        document.getElementById(
            'currenciesList'
        );


    if (!el) return;


    el.innerHTML =
        currencies.map(c => {

            const value =
                toNumber(c.value);


            const change =
                toNumber(c.change_pct) ?? 0;


            const cls =
                change >= 0
                    ? 'up'
                    : 'down';


            const arrow =
                change >= 0
                    ? '\u25b2'
                    : '\u25bc';


            return `

                <div class="market-item">

                    <div class="market-name">
                        ${c.name || ''}
                    </div>


                    <div style="text-align:right">

                        <div class="market-value">

                            ${
                                value !== null
                                    ? value.toFixed(4)
                                    : '--'
                            }

                        </div>


                        <div class="market-change ${cls}">

                            ${arrow}

                            ${Math.abs(change).toFixed(2)}%

                        </div>

                    </div>

                </div>

            `;

        }).join('');
}


/* ============================================================
   BONDS
   ============================================================ */

function renderBonds() {

    const bonds =
        dashboardData
            ? (
                dashboardData.bonds ||
                []
            )
            : [];


    const el =
        document.getElementById(
            'bondsList'
        );


    if (!el) return;


    el.innerHTML =
        bonds.map(b => {

            const yieldValue =
                toNumber(b.yield);


            const change =
                toNumber(b.change) ?? 0;


            const cls =
                change >= 0
                    ? 'up'
                    : 'down';


            const arrow =
                change >= 0
                    ? '\u25b2'
                    : '\u25bc';


            return `

                <div class="market-item">

                    <div class="market-name">
                        ${b.name || ''}
                    </div>


                    <div style="text-align:right">

                        <div class="market-value">

                            ${
                                yieldValue !== null
                                    ? yieldValue.toFixed(3) + '%'
                                    : '--'
                            }

                        </div>


                        <div class="market-change ${cls}">

                            ${arrow}

                            ${Math.abs(change).toFixed(3)}

                        </div>

                    </div>

                </div>

            `;

        }).join('');
}


/* ============================================================
   FULL MARKETS
   ============================================================ */

function renderFullMarkets() {

    const commodities =
        dashboardData
            ? (
                dashboardData.commodities ||
                []
            )
            : [];


    const currencies =
        dashboardData
            ? (
                dashboardData.currencies ||
                []
            )
            : [];


    const bonds =
        dashboardData
            ? (
                dashboardData.bonds ||
                []
            )
            : [];


    /*
     * Combine commodities and currencies.
     */
    const combined = [

        ...commodities.map(
            c => ({
                ...c,
                type: 'commodity'
            })
        ),

        ...currencies.map(
            c => ({
                ...c,
                type: 'currency'
            })
        )

    ];


    const elFull =
        document.getElementById(
            'commoditiesFullList'
        );


    if (elFull) {

        elFull.innerHTML =
            combined.map(c => {

                const value =
                    toNumber(c.value) ?? 0;


                const change =
                    toNumber(c.change_pct) ?? 0;


                const cls =
                    change >= 0
                        ? 'up'
                        : 'down';


                const arrow =
                    change >= 0
                        ? '\u25b2'
                        : '\u25bc';


                const unit =
                    c.unit
                        ? `
                            <span
                                style="
                                    font-size:10px;
                                    color:var(--text-muted)
                                "
                            >
                                ${c.unit}
                            </span>
                          `
                        : '';


                return `

                    <div class="market-item">

                        <div>

                            <div class="market-name">

                                ${c.name || ''}

                                ${unit}

                            </div>

                        </div>


                        <div style="text-align:right">

                            <div class="market-value">

                                ${
                                    c.type === 'currency'
                                        ? value.toFixed(4)
                                        : value.toLocaleString(
                                            undefined,
                                            {
                                                minimumFractionDigits: 2
                                            }
                                        )
                                }

                            </div>


                            <div class="market-change ${cls}">

                                ${arrow}

                                ${Math.abs(change).toFixed(2)}%

                            </div>

                        </div>

                    </div>

                `;

            }).join('');
    }


    /*
     * Bonds in full view.
     */
    const elBonds =
        document.getElementById(
            'bondsFullList'
        );


    if (elBonds) {

        elBonds.innerHTML =
            bonds.map(b => {

                const yieldValue =
                    toNumber(b.yield);


                const change =
                    toNumber(b.change) ?? 0;


                const cls =
                    change >= 0
                        ? 'up'
                        : 'down';


                const arrow =
                    change >= 0
                        ? '\u25b2'
                        : '\u25bc';


                return `

                    <div class="market-item">

                        <div class="market-name">
                            ${b.name || ''}
                        </div>


                        <div style="text-align:right">

                            <div class="market-value">

                                ${
                                    yieldValue !== null
                                        ? yieldValue.toFixed(3) + '%'
                                        : '--'
                                }

                            </div>


                            <div class="market-change ${cls}">

                                ${arrow}

                                ${Math.abs(change).toFixed(3)}

                            </div>

                        </div>

                    </div>

                `;

            }).join('');
    }
}
```
