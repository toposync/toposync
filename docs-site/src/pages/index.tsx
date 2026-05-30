import Heading from '@theme/Heading';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import Translate, {translate} from '@docusaurus/Translate';
import useBaseUrl from '@docusaurus/useBaseUrl';

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
        message: 'Documentation for the Toposync local-first home automation platform',
      })}>
      <main className={styles.hero}>
        <section className={styles.heroInner}>
          <img
            className={styles.symbol}
            src={symbolUrl}
            alt="Toposync symbol"
          />
          <div>
            <Heading as="h1">
              <Translate id="homepage.heading">Toposync documentation</Translate>
            </Heading>
            <p className={styles.subtitle}>
              <Translate id="homepage.subtitle">
                A local-first platform for home automation, cameras, spatial context, and extensible processing.
              </Translate>
            </p>
            <div className={styles.actions}>
              <Link className="button button--primary button--lg" to="/docs/intro">
                <Translate id="homepage.openDocs">Open docs</Translate>
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
