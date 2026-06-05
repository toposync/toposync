import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const siteUrl = 'https://docs.toposync.com';
const productUrl = 'https://toposync.com';
const socialImageUrl = `${siteUrl}/img/social-card.png`;
const seoDescription =
  'Documentation for Toposync, an open source project for local-first Spatial Home Automation with local intelligence, Spatial Camera Mapping, Spatial Events, Home Assistant, pipelines, and processing servers.';

const config: Config = {
  title: 'Toposync',
  tagline: 'Spatial Home Automation with local intelligence.',
  favicon: 'img/favicon.png',

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // Set the production url of your site here
  url: 'https://docs.toposync.com',
  // Set the /<baseUrl>/ pathname under which your site is served
  // For GitHub pages deployment, it is often '/<projectName>/'
  baseUrl: '/',

  // GitHub pages deployment config.
  // If you aren't using GitHub pages, you don't need these.
  organizationName: 'toposync',
  projectName: 'toposync',

  onBrokenLinks: 'throw',

  // Even if you don't use internationalization, you can use this field to set
  // useful metadata like html lang. For example, if your site is Chinese, you
  // may want to replace "en" with "zh-Hans".
  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'pt-BR'],
    localeConfigs: {
      en: {
        label: 'English',
        htmlLang: 'en',
      },
      'pt-BR': {
        label: 'Português (Brasil)',
        htmlLang: 'pt-BR',
      },
    },
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl:
            'https://github.com/toposync/toposync/edit/main/docs-site/',
        },
        blog: false,
        sitemap: {
          lastmod: 'date',
          changefreq: 'weekly',
          priority: 0.7,
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    metadata: [
      {
        name: 'keywords',
        content:
          'Toposync, Spatial Home Automation, Spatial Camera Mapping, Spatial Events, Spatial Intelligence, Spatial Awareness, home automation, Home Assistant, cameras, RTSP, ONVIF, PTZ, local-first, processing server, computer vision',
      },
      {name: 'author', content: 'Toposync'},
      {name: 'application-name', content: 'Toposync'},
      {name: 'twitter:card', content: 'summary_large_image'},
      {name: 'twitter:image', content: socialImageUrl},
      {name: 'twitter:title', content: 'Toposync documentation'},
      {name: 'twitter:description', content: seoDescription},
      {property: 'og:type', content: 'website'},
      {property: 'og:site_name', content: 'Toposync Docs'},
      {property: 'og:image', content: socialImageUrl},
      {property: 'og:image:width', content: '1200'},
      {property: 'og:image:height', content: '630'},
    ],
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Toposync',
      logo: {
        alt: 'Toposync symbol',
        src: 'img/toposync-symbol.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          type: 'localeDropdown',
          position: 'right',
        },
        {
          href: 'https://github.com/toposync/toposync',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {
              label: 'Introduction',
              to: '/docs/intro',
            },
          ],
        },
        {
          title: 'More',
          items: [
            {
              label: 'GitHub',
              href: 'https://github.com/toposync/toposync',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Toposync. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
  } satisfies Preset.ThemeConfig,
  headTags: [
    {
      tagName: 'script',
      attributes: {
        type: 'application/ld+json',
      },
      innerHTML: JSON.stringify({
        '@context': 'https://schema.org',
        '@type': 'Organization',
        name: 'Toposync',
        url: productUrl,
        logo: `${siteUrl}/img/toposync-symbol.svg`,
        sameAs: ['https://github.com/toposync/toposync'],
      }),
    },
    {
      tagName: 'script',
      attributes: {
        type: 'application/ld+json',
      },
      innerHTML: JSON.stringify({
        '@context': 'https://schema.org',
        '@type': 'WebSite',
        name: 'Toposync Docs',
        url: siteUrl,
        inLanguage: ['en', 'pt-BR'],
        publisher: {
          '@type': 'Organization',
          name: 'Toposync',
        },
      }),
    },
    {
      tagName: 'script',
      attributes: {
        type: 'application/ld+json',
      },
      innerHTML: JSON.stringify({
        '@context': 'https://schema.org',
        '@type': 'SoftwareApplication',
        name: 'Toposync',
        applicationCategory: 'HomeApplication',
        operatingSystem: 'Linux, macOS, Windows, Docker, Home Assistant OS',
        url: productUrl,
        image: socialImageUrl,
        description: seoDescription,
        softwareHelp: `${siteUrl}/docs/`,
      }),
    },
  ],
};

export default config;
