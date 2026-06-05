import Heading from '@theme/Heading';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import Translate, {translate} from '@docusaurus/Translate';

import styles from './index.module.css';

export default function Home(): JSX.Element {
  return (
    <Layout
      title={translate({
        id: 'homepage.title',
        message: 'Toposync documentation',
      })}
      description={translate({
        id: 'homepage.description',
        message:
          'Documentation for Toposync, an open source project for local-first Spatial Home Automation with local intelligence, cameras, spatial events, Home Assistant OS, pipelines, and processing servers.',
      })}>
      <main className={styles.home}>
        <section className={styles.intro}>
          <div className={styles.container}>
            <p className={styles.alphaBadge}>
              <Translate id="homepage.alphaBadge">Alpha documentation</Translate>
            </p>
            <Heading as="h1" className={styles.title}>
              <Translate id="homepage.heading">Toposync documentation</Translate>
            </Heading>
            <p className={styles.introText}>
              <Translate id="homepage.introText">
                Practical guides for installing Toposync, creating your first spatial composition,
                connecting cameras, and understanding Spatial Home Automation with local intelligence in the current architecture.
              </Translate>
            </p>
            <div className={styles.actions}>
              <Link className="button button--primary button--lg" to="/docs/installation/choose-your-installation">
                <Translate id="homepage.chooseInstall">Choose installation</Translate>
              </Link>
              <Link className="button button--secondary button--lg" to="/docs/first-steps/">
                <Translate id="homepage.startFirstSteps">Start with first steps</Translate>
              </Link>
            </div>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="start-here">
          <div className={styles.container}>
            <div className={styles.sectionHeader}>
              <Heading as="h2" id="start-here">
                <Translate id="homepage.startHere.title">Start here</Translate>
              </Heading>
              <p>
                <Translate id="homepage.startHere.description">
                  Pick the guide that matches what you are trying to do right now.
                </Translate>
              </p>
            </div>

            <div className={styles.primaryGrid}>
              <Link className={styles.guideCard} to="/docs/installation/choose-your-installation">
                <span className={styles.cardKicker}>
                  <Translate id="homepage.install.kicker">Installation</Translate>
                </span>
                <Heading as="h3">
                  <Translate id="homepage.install.title">Choose your installation</Translate>
                </Heading>
                <p>
                  <Translate id="homepage.install.description">
                    Decide between Python, Docker, Home Assistant OS, GPU upgrades, and processing servers.
                  </Translate>
                </p>
              </Link>

              <Link className={styles.guideCard} to="/docs/first-steps/">
                <span className={styles.cardKicker}>
                  <Translate id="homepage.firstSteps.kicker">First use</Translate>
                </span>
                <Heading as="h3">
                  <Translate id="homepage.firstSteps.title">Build your first composition</Translate>
                </Heading>
                <p>
                  <Translate id="homepage.firstSteps.description">
                    Start with a tracing image, add walls and areas, place cameras, and create a simple pipeline.
                  </Translate>
                </p>
              </Link>

              <Link className={styles.guideCard} to="/docs/installation/home-assistant-addon">
                <span className={styles.cardKicker}>
                  <Translate id="homepage.homeAssistant.kicker">Home Assistant OS</Translate>
                </span>
                <Heading as="h3">
                  <Translate id="homepage.homeAssistant.title">Install the add-on</Translate>
                </Heading>
                <p>
                  <Translate id="homepage.homeAssistant.description">
                    Use the supervised add-on path with sidebar ingress, direct access options, and local Home Assistant integration.
                  </Translate>
                </p>
              </Link>
            </div>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="browse-by-task">
          <div className={styles.container}>
            <div className={styles.sectionHeader}>
              <Heading as="h2" id="browse-by-task">
                <Translate id="homepage.browse.title">Browse by task</Translate>
              </Heading>
              <p>
                <Translate id="homepage.browse.description">
                  Use these entry points when you already know which part of Toposync you need.
                </Translate>
              </p>
            </div>

            <div className={styles.taskGrid}>
              <Link className={styles.taskLink} to="/docs/cameras/overview">
                <span>
                  <Translate id="homepage.task.cameras.title">Cameras</Translate>
                </span>
                <small>
                  <Translate id="homepage.task.cameras.description">RTSP, ONVIF, Spatial Camera Mapping, and image processing</Translate>
                </small>
              </Link>
              <Link className={styles.taskLink} to="/docs/installation/processing-server-linux-macos">
                <span>
                  <Translate id="homepage.task.processing.title">Processing servers</Translate>
                </span>
                <small>
                  <Translate id="homepage.task.processing.description">Move heavier camera and vision workloads to another machine</Translate>
                </small>
              </Link>
              <Link className={styles.taskLink} to="/docs/home-assistant-addon/overview">
                <span>
                  <Translate id="homepage.task.homeAssistant.title">Home Assistant add-on details</Translate>
                </span>
                <small>
                  <Translate id="homepage.task.homeAssistant.description">Ingress, operation, configuration, and troubleshooting</Translate>
                </small>
              </Link>
              <Link className={styles.taskLink} to="/docs/reference/configuration">
                <span>
                  <Translate id="homepage.task.reference.title">Reference</Translate>
                </span>
                <small>
                  <Translate id="homepage.task.reference.description">Configuration, ports, environment variables, and file locations</Translate>
                </small>
              </Link>
              <Link className={styles.taskLink} to="/docs/developers/architecture">
                <span>
                  <Translate id="homepage.task.developers.title">Developers</Translate>
                </span>
                <small>
                  <Translate id="homepage.task.developers.description">Architecture, Spatial Events, extension authoring, plugin API, and pipelines</Translate>
                </small>
              </Link>
              <Link className={styles.taskLink} to="/docs/troubleshooting/">
                <span>
                  <Translate id="homepage.task.troubleshooting.title">Troubleshooting</Translate>
                </span>
                <small>
                  <Translate id="homepage.task.troubleshooting.description">Known failure modes and practical checks</Translate>
                </small>
              </Link>
            </div>
          </div>
        </section>

        <section className={styles.noteSection}>
          <div className={styles.container}>
            <p className={styles.alphaNote}>
              <strong>
                <Translate id="homepage.alphaNote.label">Early access alpha.</Translate>
              </strong>{' '}
              <Translate id="homepage.alphaNote.text">
                Test Toposync in contained environments and follow the security guidance before using cameras,
                Home Assistant entities, or automation workflows.
              </Translate>
            </p>
          </div>
        </section>
      </main>
    </Layout>
  );
}
