import Heading from '@theme/Heading';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';

import styles from './index.module.css';

export default function Home(): JSX.Element {
  return (
    <Layout
      title="Toposync documentation"
      description="Documentation for the Toposync local-first home automation platform">
      <main className={styles.hero}>
        <section className={styles.heroInner}>
          <img
            className={styles.symbol}
            src="/img/toposync-symbol.svg"
            alt="Toposync symbol"
          />
          <div>
            <Heading as="h1">Toposync documentation</Heading>
            <p className={styles.subtitle}>
              A local-first platform for home automation, cameras, spatial context, and extensible processing.
            </p>
            <div className={styles.actions}>
              <Link className="button button--primary button--lg" to="/docs/intro">
                Open docs
              </Link>
              <Link className="button button--secondary button--lg" to="https://github.com/toposync/toposync">
                GitHub
              </Link>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
