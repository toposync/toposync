import Heading from '@theme/Heading';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import Translate, {translate} from '@docusaurus/Translate';
import useBaseUrl from '@docusaurus/useBaseUrl';
import type {CSSProperties} from 'react';

import styles from './index.module.css';

export default function Home(): JSX.Element {
  const symbolUrl = useBaseUrl('img/toposync-symbol.svg');

  return (
    <Layout
      title={translate({
        id: 'homepage.title',
        message: 'Toposync documentation',
      })}
      description={translate({
        id: 'homepage.description',
        message: 'Documentation for the Toposync local-first spatial home automation platform',
      })}>
      <main className={styles.home}>
        <section className={styles.hero}>
          <div className={styles.heroInner}>
            <div>
              <div className={styles.eyebrow}>
                <span className={styles.eyebrowDot} />
                <Translate id="homepage.eyebrow">Alpha docs · Local-first spatial operations</Translate>
              </div>
              <Heading as="h1" className={styles.title}>
                <Translate id="homepage.headingLine1">Map your home.</Translate>
                <span className={styles.titleAccent}>
                  <Translate id="homepage.headingLine2">Operate with context.</Translate>
                </span>
              </Heading>
              <p className={styles.subtitle}>
                <Translate id="homepage.subtitle">
                  Practical documentation for installing Toposync, creating your first spatial composition,
                  connecting cameras, and building local-first automation workflows.
                </Translate>
              </p>
              <div className={styles.actions}>
                <Link className="button button--primary button--lg" to="/docs/first-steps/">
                  <Translate id="homepage.startFirstSteps">Start with first steps</Translate>
                </Link>
                <Link className="button button--secondary button--lg" to="/docs/installation/choose-your-installation">
                  <Translate id="homepage.chooseInstall">Choose installation</Translate>
                </Link>
              </div>
              <ul className={styles.quickStats} aria-label={translate({id: 'homepage.highlights', message: 'Highlights'})}>
                <li>
                  <Translate id="homepage.stat.localFirst">Local-first</Translate>
                </li>
                <li>
                  <Translate id="homepage.stat.ha">Home Assistant ready</Translate>
                </li>
                <li>
                  <Translate id="homepage.stat.extensions">Extension runtime</Translate>
                </li>
              </ul>
            </div>

            <aside className={styles.controlPanel} aria-label={translate({id: 'homepage.panel.label', message: 'Toposync system overview'})}>
              <div className={styles.symbolFrame}>
                <img className={styles.symbol} src={symbolUrl} alt="Toposync symbol" />
              </div>
              <div className={styles.panelRows}>
                <div className={styles.panelRow} style={{'--row-color': 'var(--topo-teal)'} as CSSProperties}>
                  <span className={styles.panelDot} />
                  <span className={styles.panelLabel}>
                    <Translate id="homepage.panel.composition">Composition</Translate>
                  </span>
                  <span className={styles.panelValue}>2D/3D</span>
                </div>
                <div className={styles.panelRow} style={{'--row-color': 'var(--topo-blue)'} as CSSProperties}>
                  <span className={styles.panelDot} />
                  <span className={styles.panelLabel}>
                    <Translate id="homepage.panel.cameras">Cameras</Translate>
                  </span>
                  <span className={styles.panelValue}>RTSP / ONVIF</span>
                </div>
                <div className={styles.panelRow} style={{'--row-color': 'var(--topo-amber)'} as CSSProperties}>
                  <span className={styles.panelDot} />
                  <span className={styles.panelLabel}>
                    <Translate id="homepage.panel.pipelines">Pipelines</Translate>
                  </span>
                  <span className={styles.panelValue}>LOCAL CPU</span>
                </div>
              </div>
            </aside>
          </div>
        </section>

        <section className={styles.cards} aria-label={translate({id: 'homepage.cards.label', message: 'Documentation entry points'})}>
          <Link className={styles.card} to="/docs/installation/choose-your-installation" style={{'--card-color': 'var(--topo-gradient-accent)'} as CSSProperties}>
            <span className={styles.cardKicker}>
              <Translate id="homepage.card.install.kicker">Install</Translate>
            </span>
            <Heading as="h2" className={styles.cardTitle}>
              <Translate id="homepage.card.install.title">Pick the right deployment path</Translate>
            </Heading>
            <p className={styles.cardDescription}>
              <Translate id="homepage.card.install.description">
                Python, Docker, CUDA, Windows services, Home Assistant add-on, and processing servers.
              </Translate>
            </p>
          </Link>
          <Link className={styles.card} to="/docs/first-steps/" style={{'--card-color': 'var(--topo-amber)'} as CSSProperties}>
            <span className={styles.cardKicker}>
              <Translate id="homepage.card.firstSteps.kicker">First run</Translate>
            </span>
            <Heading as="h2" className={styles.cardTitle}>
              <Translate id="homepage.card.firstSteps.title">Create the first composition</Translate>
            </Heading>
            <p className={styles.cardDescription}>
              <Translate id="homepage.card.firstSteps.description">
                Start with a tracing image, add areas, place a camera, and build the first simple flow.
              </Translate>
            </p>
          </Link>
          <Link className={styles.card} to="/docs/home-assistant-addon/overview" style={{'--card-color': 'var(--topo-green)'} as CSSProperties}>
            <span className={styles.cardKicker}>
              <Translate id="homepage.card.homeAssistant.kicker">Home Assistant</Translate>
            </span>
            <Heading as="h2" className={styles.cardTitle}>
              <Translate id="homepage.card.homeAssistant.title">Run inside your HA environment</Translate>
            </Heading>
            <p className={styles.cardDescription}>
              <Translate id="homepage.card.homeAssistant.description">
                Sidebar ingress, supervised operation, direct-port access, and local Core API integration.
              </Translate>
            </p>
          </Link>
          <Link className={styles.card} to="https://github.com/toposync/toposync" style={{'--card-color': 'var(--topo-cyan)'} as CSSProperties}>
            <span className={styles.cardKicker}>GitHub</span>
            <Heading as="h2" className={styles.cardTitle}>
              <Translate id="homepage.card.github.title">Follow the alpha</Translate>
            </Heading>
            <p className={styles.cardDescription}>
              <Translate id="homepage.card.github.description">
                Report issues, inspect the source, contribute docs, and help validate real local setups.
              </Translate>
            </p>
          </Link>
        </section>
      </main>
    </Layout>
  );
}
