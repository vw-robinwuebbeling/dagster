import {Box, Icon} from '@dagster-io/ui-components';
import {Link} from 'react-router-dom';

import styles from './css/MarketplaceLink.module.css';

export const MarketplaceLink = () => {
  return (
    <Link className={styles.marketplaceLink} to="/integrations">
      <Box
        flex={{direction: 'row', alignItems: 'center', gap: 8, justifyContent: 'center'}}
        className={styles.marketplaceLinkContent}
      >
        <Icon name="compute_kind" />
        Integrations
      </Box>
    </Link>
  );
};
